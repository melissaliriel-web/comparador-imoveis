import math
import streamlit as st
import pandas as pd
import pdfplumber
import subprocess
import sys
import json
import os
import re
import tempfile

# Instala o navegador do Playwright automaticamente (necessário no Streamlit Cloud)
def _ensure_playwright():
    marker = os.path.join(tempfile.gettempdir(), ".pw_installed")
    if not os.path.exists(marker):
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"],
                       capture_output=True)
        open(marker, "w").close()

_ensure_playwright()


def _safe_str(val):
    """Converte pandas NaN / None para string vazia de forma segura."""
    if val is None:
        return ""
    try:
        if math.isnan(float(val)):
            return ""
    except (TypeError, ValueError):
        pass
    s = str(val).strip()
    return "" if s.lower() in ("nan", "none") else s

st.set_page_config(page_title="Comparador de Tabela Imobiliária", page_icon="🏠", layout="wide")
st.title("🏠 Comparador de Tabela Imobiliária")

BOT = os.path.join(os.path.dirname(__file__), "bot.py")

# ── helpers ───────────────────────────────────────────────────────────────────

def parse_money(val):
    if val is None or str(val).strip() in ("", "-", "nan"):
        return None
    s = re.sub(r"[R$\s]", "", str(val)).replace(".", "").replace(",", ".")
    try:
        return float(s)
    except Exception:
        return None

def fmt_money(val):
    if val is None:
        return "—"
    return f"R$ {val:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

def fmt_money_san(val_str):
    v = parse_money(val_str)
    if v is None:
        return val_str
    return f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

def normalize_status(s):
    if not s:
        return "desconhecido"
    s = str(s).lower().strip()
    # "à venda" / "a venda" = disponível — precisa checar ANTES de "vend"
    if re.search(r"[aà]\s*venda", s):
        return "disponivel"
    # "vendido" E "vendida" (feminino usado em tabelas de imóveis)
    if any(x in s for x in ["vendid", "sold", "indispon"]):
        return "vendido"
    if "reserv" in s:
        return "reservado"
    # "Disponível" (com acento), "disponivel", "Promoção", "livre", etc.
    if any(x in s for x in ["disp", "promo", "livre", "avail", "ativ", "ok"]):
        return "disponivel"
    return s

def detect_tem_status(df):
    """Retorna True se o DataFrame parece ter uma coluna com valores de status (vendido/à venda)."""
    # "dispon" bate com "disponível" (í acentuado) e "disponivel" sem acento
    status_re = re.compile(r"(?i)(vendid|[aà]\s*venda|dispon|promo|inativ|reserv|sold)")
    for col in df.columns:
        hits = df[col].apply(lambda v: bool(status_re.search(str(v) if v is not None else ""))).sum()
        if hits >= 1:
            return True
    return False

def _row_is_vendida(row):
    """True se qualquer célula da linha contiver 'VENDIDA' ou 'VENDIDO' (independente da coluna)."""
    return any(re.search(r"(?i)vendid", str(v)) for v in row.values)

def detect_col(df_or_cols, keywords):
    """Auto-detecção por nome de coluna. Aceita DataFrame ou lista de nomes."""
    cols = df_or_cols.columns.tolist() if hasattr(df_or_cols, "columns") else list(df_or_cols)
    for kw in keywords:
        for c in cols:
            if kw in str(c).lower():
                return c
    return cols[0]

def _norm_col_name(c):
    """Normaliza nome de coluna: remove quebras de linha, espaços duplos e acentos simples."""
    s = re.sub(r"[\n\r\t]+", " ", str(c))
    s = re.sub(r" +", " ", s).strip().lower()
    # Remove acentos comuns para comparação
    for a, b in [("ã","a"),("â","a"),("á","a"),("à","a"),("ê","e"),("é","e"),
                 ("í","i"),("õ","o"),("ô","o"),("ó","o"),("ú","u"),("ç","c")]:
        s = s.replace(a, b)
    return s

def detect_valor_col(df):
    """
    Auto-detecção da coluna de VALOR TOTAL DE VENDA.
    1. Nome da coluna com palavras-chave de preço total.
    2. Conteúdo: entre colunas com valores em formato de preço, escolhe a de MAIOR MÉDIA
       (preço de apartamento ~500k >> sinal ~60k >> área ~120).
       Valores com média < 100.000 são ignorados (parcelas, metragem, etc.).
    3. Fallback: maior média numérica absoluta.
    """
    cols = df.columns.tolist()

    # Padrão de exclusão por nome (parcelas, sinal, etc.)
    _excl = re.compile(
        r"(?i)(sinal|parcela|entrada|mensal|semestral|financ|enxoval|igpm|incc|permuta|desconto|correcao)")

    # 1. Nome da coluna (normalizado, sem acentos, sem quebras)
    _price_kws = [
        "preco de venda", "valor de venda",
        "preco total",    "valor total",
        "valor tabela",   "preco venda",
        "preco",          "valor",
    ]
    for kw in _price_kws:
        for col in cols:
            col_n = _norm_col_name(col)
            if kw in col_n and not _excl.search(col_n):
                return col

    # 2. Conteúdo: separador de milhar OU R$ — pega a coluna com MAIOR MÉDIA acima de 100k
    money_re = re.compile(r"R\$|\d{1,3}(\.\d{3})+,\d{2}")
    MIN_IMOVEL = 100_000  # abaixo disso provavelmente é sinal, parcela ou área

    candidates = []
    for col in cols:
        if _excl.search(_norm_col_name(col)):
            continue
        has_fmt = df[col].apply(
            lambda v: bool(money_re.search(str(v) if v is not None else ""))).sum()
        if has_fmt == 0:
            continue
        vals = df[col].apply(parse_money).dropna()
        if len(vals) > 0 and vals.mean() >= MIN_IMOVEL:
            candidates.append((col, vals.mean()))

    if candidates:
        return max(candidates, key=lambda x: x[1])[0]  # maior média = preço total

    # 3. Fallback: maior média numérica entre todas as colunas não excluídas
    best_num, best_col = 0, cols[0]
    for col in cols:
        if _excl.search(_norm_col_name(col)):
            continue
        vals = df[col].apply(parse_money).dropna()
        if len(vals) > 0 and vals.mean() > best_num:
            best_num, best_col = vals.mean(), col
    return best_col

def _palavras_em_linhas(pdf):
    """Coleta todas as palavras do PDF agrupadas em linhas por posição Y."""
    TOL_Y = 4
    all_words = []
    y_off = 0.0
    for page in pdf.pages:
        try:
            ws = page.extract_words(x_tolerance=3, y_tolerance=3,
                                    keep_blank_chars=False, use_text_flow=False)
        except Exception:
            ws = []
        for w in ws:
            all_words.append({
                'text': w['text'],
                'x0': float(w['x0']), 'x1': float(w['x1']),
                'yc': (float(w['top']) + float(w['bottom'])) / 2 + y_off,
            })
        y_off += float(page.height)
    if not all_words:
        return []
    all_words.sort(key=lambda w: w['yc'])
    lines, cur, cur_y = [], [all_words[0]], all_words[0]['yc']
    for w in all_words[1:]:
        if abs(w['yc'] - cur_y) <= TOL_Y:
            cur.append(w)
        else:
            lines.append(sorted(cur, key=lambda w: w['x0']))
            cur, cur_y = [w], w['yc']
    lines.append(sorted(cur, key=lambda w: w['x0']))
    return lines

def _extract_por_palavras(pdf):
    """
    Reconstrói a tabela usando coordenadas X/Y das palavras.
    Funciona para PDFs com fundo colorido sem linhas vetoriais (ex: UNO).
    """
    COL_GAP = 10  # gap mínimo entre colunas no cabeçalho (px)
    lines = _palavras_em_linhas(pdf)
    if not lines:
        return None, "Sem conteúdo."

    # Localiza linha de cabeçalho
    hdr_idx = -1
    for i, line in enumerate(lines):
        ts = {w['text'].lower() for w in line}
        if 'unidade' in ts or ('tipologia' in ts and 'status' in ts):
            hdr_idx = i
            break
    if hdr_idx < 0:
        return None, None

    # Define colunas agrupando palavras adjacentes do cabeçalho
    col_defs, g_name, g_x0, prev_x1 = [], '', None, None
    for w in lines[hdr_idx]:
        gap = (w['x0'] - prev_x1) if prev_x1 is not None else 0
        if prev_x1 is not None and gap > COL_GAP:
            col_defs.append({'name': g_name.strip(), 'x0': g_x0})
            g_name, g_x0 = w['text'], w['x0']
        else:
            g_name = ((g_name + ' ' + w['text']).strip()) if g_name else w['text']
            if g_x0 is None:
                g_x0 = w['x0']
        prev_x1 = w['x1']
    if g_name:
        col_defs.append({'name': g_name.strip(), 'x0': g_x0})
    for i, c in enumerate(col_defs):
        c['x1'] = (col_defs[i+1]['x0'] - 1) if i + 1 < len(col_defs) else float('inf')

    hdr_set = {w['text'] for w in lines[hdr_idx]}
    n_cols = len(col_defs)
    col_names = [c['name'] for c in col_defs]
    rows = []

    for line in lines[hdr_idx + 1:]:
        line_set = {w['text'] for w in line}
        # Pula repetições do cabeçalho (páginas seguintes)
        if len(line_set & hdr_set) >= max(2, n_cols // 2):
            continue
        row = [''] * n_cols
        for w in line:
            wx = (w['x0'] + w['x1']) / 2
            placed = False
            for ci, col in enumerate(col_defs):
                if col['x0'] <= wx <= col['x1']:
                    row[ci] = (row[ci] + ' ' + w['text']).strip()
                    placed = True
                    break
            if not placed:
                dists = [abs(wx - (c['x0'] + c['x1']) / 2) for c in col_defs]
                best = dists.index(min(dists))
                row[best] = (row[best] + ' ' + w['text']).strip()
        rows.append(row)

    if not rows:
        return None, "Nenhuma linha encontrada."
    df = pd.DataFrame(rows, columns=col_names)
    df = df[df.apply(
        lambda r: any(str(v).strip() not in ('', '-', '--', 'nan') for v in r), axis=1
    )].reset_index(drop=True)
    return df, None

def extract_pdf(file):
    with pdfplumber.open(file) as pdf:
        # Método 1: extract_tables com estratégias múltiplas
        rows, header = [], None
        for page in pdf.pages:
            for cfg in [{}, {"vertical_strategy": "text", "horizontal_strategy": "text",
                             "snap_tolerance": 3, "join_tolerance": 3}]:
                try:
                    tables = page.extract_tables(table_settings=cfg) or []
                except Exception:
                    tables = []
                if tables:
                    break
            for table in tables:
                if not table:
                    continue
                primeira = [str(c).strip() if c else "" for c in table[0]]
                if header is None:
                    header = [c if c else f"col_{i}" for i, c in enumerate(primeira)]
                    data_rows = table[1:]
                else:
                    data_rows = table[1:] if primeira == header else table
                for row in data_rows:
                    if not row:
                        continue
                    linha = [str(c).strip() if c else "" for c in row]
                    if len(linha) < len(header):
                        linha += [""] * (len(header) - len(linha))
                    elif len(linha) > len(header):
                        linha = linha[:len(header)]
                    rows.append(linha)

        # Método 2: por posição de palavras (robusto para PDFs sem linhas)
        df_palavras, _ = _extract_por_palavras(pdf)

        n_table = len(rows)
        n_words = len(df_palavras) if df_palavras is not None else 0

        # Usa o método que extraiu mais linhas
        if n_table >= n_words and n_table > 0 and header:
            return (pd.DataFrame(rows, columns=header)
                    .dropna(how="all").reset_index(drop=True), None)
        if df_palavras is not None and n_words > 0:
            return df_palavras, None
        return None, "Nenhuma tabela detectada no PDF."

def normaliza_unid(s):
    """
    Extrai o número da unidade removendo prefixos textuais e zeros à esquerda.
    Exemplos: '01'→'1', 'Loja 01'→'1', 'Área Priv. 201'→'201', '1101 COB'→'1101'
    """
    s = str(s).strip()
    # Remove prefixos textuais antes do número
    s = re.sub(
        r"(?i)^(?:apto?\.?\s*(?:tipo\.?\s*)?|apartamento\s*|unidade\s*|lote\s*|sala\s*"
        r"|cob(?:ertura)?\s*|ph\s*|penthouse\s*|loja\s*"
        r"|[aá]rea\s+priv(?:ativa)?\.?\s*|ap\.?\s*)",
        "", s
    ).strip()
    # Número no início (1–6 dígitos); str(int()) remove zeros à esquerda p/ comparação uniforme
    m = re.match(r'^(\d{1,6})', s)
    if m:
        return str(int(m.group(1)))
    # Fallback: primeiro número de 2+ dígitos em qualquer posição (ex: 'Apto. Tipo 301')
    m = re.search(r'(\d{2,6})', s)
    return str(int(m.group(1))) if m else s

def is_valid_unit(val):
    """True se val parece um número de unidade (1–6 dígitos, não-zero, após normalização)."""
    n = normaliza_unid(str(val))
    return bool(re.match(r'^[1-9]\d{0,5}$', n))

def extrair_tipo_secao(texto):
    """Extrai tipo de apartamento do cabeçalho de seção do PDF (ex: '2 quartos', 'studio')."""
    t = str(texto).lower()
    if any(s in t for s in ["studio", "loft", "kitnet", "kitinete"]):
        return "studio"
    m = re.search(r"(\d+)\s*quartos?", t)
    if m:
        n = int(m.group(1))
        return f"{n} quarto{'s' if n != 1 else ''}"
    return None

def enrich_df_com_tipo(df, col_unid):
    """Adiciona coluna '_tipo_pdf' propagando o tipo de seção para cada unidade."""
    secao_atual = None
    tipos = []
    for _, row in df.iterrows():
        val = str(row[col_unid]).strip()
        if not is_valid_unit(val) and len(val) > 5:
            t = extrair_tipo_secao(val)
            if t:
                secao_atual = t
        tipos.append(secao_atual)
    df = df.copy()
    df["_tipo_pdf"] = tipos
    return df

def compare(df_c, col_c_unid, col_c_status, col_c_valor, sem_status, df_san):
    """
    Compara a tabela da construtora com os dados do SAN.

    sem_status=True  → o PDF só lista unidades disponíveis (sem coluna de status).
                        Unidades no SAN que NÃO aparecem no PDF = vendidas → REMOVER.
    sem_status=False → usa a coluna de status do PDF para decidir.
    """
    # Enriquece com tipo de seção e filtra apenas linhas de unidade válida
    df_c = enrich_df_com_tipo(df_c, col_c_unid)
    df_c = df_c[df_c[col_c_unid].apply(is_valid_unit)].copy().reset_index(drop=True)

    resultados = []

    # Monta conjunto de unidades do PDF (para verificar o inverso depois)
    unids_pdf = set()
    for _, row_c in df_c.iterrows():
        unids_pdf.add(normaliza_unid(str(row_c[col_c_unid])))

    # ── SAN → PDF ────────────────────────────────────────────────────────────
    for _, row_s in df_san.iterrows():
        # Matching é feito pelo número da unidade, não pelo código SAN
        unid_san  = _safe_str(row_s.get("unidade"))
        cod_san   = _safe_str(row_s.get("codigo"))
        valor_san = parse_money(row_s.get("valor_raw"))
        captacao  = _safe_str(row_s.get("captacao"))

        # Captação "outro" = não é nossa, ignorar completamente
        if captacao not in ("associada", "repasse"):
            continue

        # Sem número de unidade identificado → não conseguimos comparar
        if not unid_san:
            resultados.append({
                "Unidade": f"(COD {cod_san})", "COD SAN": cod_san,
                "Ação": "❓ SEM UNIDADE",
                "Motivo": "Olhinho nao revelou o numero da unidade — verificar manualmente",
                "Captacao": captacao or "—",
                "Valor SAN": fmt_money(valor_san), "Valor Construtora": "—",
                "_cod": None, "_novo_valor": None,
            })
            continue

        # Match na tabela da construtora pelo número da unidade
        n_san = normaliza_unid(unid_san)
        match = df_c[df_c[col_c_unid].apply(lambda x: normaliza_unid(str(x))) == n_san]

        if match.empty:
            # Não está na tabela da construtora = foi vendida → REMOVER do SAN
            if captacao == "associada":
                acao, _cod = "🔴 REMOVER", cod_san
            else:
                acao, _cod = "⚠️ REPASSE — verificar manualmente", None
            resultados.append({
                "Unidade": unid_san, "COD SAN": cod_san,
                "Ação": acao,
                "Motivo": "Nao consta na tabela da construtora — provavelmente vendida",
                "Captacao": captacao or "—",
                "Valor SAN": fmt_money(valor_san), "Valor Construtora": "—",
                "_cod": _cod, "_novo_valor": None,
            })
            continue

        row_c   = match.iloc[0]
        valor_c = parse_money(row_c[col_c_valor])

        # Verifica se o tipo (quartos) bate entre o PDF e o SAN
        tipo_pdf  = row_c.get("_tipo_pdf")
        quartos_san = row_s.get("quartos")
        if tipo_pdf and quartos_san is not None:
            if tipo_pdf == "studio":
                esperado = 0
            else:
                m_q = re.match(r"(\d+)", tipo_pdf)
                esperado = int(m_q.group(1)) if m_q else None
            if esperado is not None and quartos_san != esperado:
                resultados.append({
                    "Unidade": unid_san, "COD SAN": cod_san,
                    "Ação": "🟠 TIPO DIFERENTE",
                    "Motivo": (f"PDF indica '{tipo_pdf}' mas SAN tem {quartos_san} "
                               f"quarto{'s' if quartos_san != 1 else ''}"),
                    "Captacao": captacao or "—",
                    "Valor SAN": fmt_money(valor_san), "Valor Construtora": fmt_money(valor_c),
                    "_cod": None, "_novo_valor": None,
                })
                continue

        # Verifica status sempre que a coluna for detectada (independente de sem_status)
        status = normalize_status(row_c[col_c_status]) if col_c_status else "desconhecido"

        # Override universal: qualquer célula da linha contém "VENDIDA"/"VENDIDO"
        if status == "desconhecido" and _row_is_vendida(row_c):
            status = "vendido"

        # Fallback: pdfplumber às vezes não lê texto colorido (ex: VENDIDO em vermelho).
        # Se tem coluna de status mas ficou vazio E não há preço → assume VENDIDO.
        if status == "desconhecido" and col_c_status and col_c_valor:
            if parse_money(row_c.get(col_c_valor, "")) is None:
                status = "vendido"

        if status in ("vendido", "reservado"):
                resultados.append({
                    "Unidade": unid_san, "COD SAN": cod_san,
                    "Ação": "🔴 REMOVER" if captacao == "associada" else "⚠️ REPASSE",
                    "Motivo": f"Status na construtora: {status}",
                    "Captacao": captacao or "—",
                    "Valor SAN": fmt_money(valor_san), "Valor Construtora": fmt_money(valor_c),
                    "_cod": cod_san if captacao == "associada" else None,
                    "_novo_valor": None,
                })
                continue

        if valor_c is not None and valor_san is not None and abs(valor_c - valor_san) > 0.99:
            resultados.append({
                "Unidade": unid_san, "COD SAN": cod_san,
                "Ação": "🟡 ATUALIZAR VALOR" if captacao == "associada" else "⚠️ REPASSE — verificar",
                "Motivo": f"{fmt_money(valor_san)} -> {fmt_money(valor_c)}",
                "Captacao": captacao or "—",
                "Valor SAN": fmt_money(valor_san), "Valor Construtora": fmt_money(valor_c),
                "_cod": cod_san if captacao == "associada" else None,
                "_novo_valor": fmt_money_san(row_c[col_c_valor]) if captacao == "associada" else None,
            })
        else:
            resultados.append({
                "Unidade": unid_san, "COD SAN": cod_san,
                "Ação": "🟢 OK",
                "Motivo": "Disponivel e valor correto",
                "Captacao": captacao or "—",
                "Valor SAN": fmt_money(valor_san), "Valor Construtora": fmt_money(valor_c),
                "_cod": None, "_novo_valor": None,
            })

    # ── PDF → SAN ────────────────────────────────────────────────────────────
    # Dois conjuntos: unidades da NOSSA captação vs TODAS as unidades no SAN
    unids_nossa_captacao = set()
    unids_san_todos      = set()
    for _, row_s in df_san.iterrows():
        u_raw = str(row_s.get("unidade") or "").strip()
        u     = normaliza_unid(u_raw)
        if not u:
            continue
        unids_san_todos.add(u)
        if str(row_s.get("captacao") or "") in ("associada", "repasse"):
            unids_nossa_captacao.add(u)

    for _, row_c in df_c.iterrows():
        unid_pdf = normaliza_unid(str(row_c[col_c_unid]))
        if not unid_pdf or unid_pdf in unids_nossa_captacao:
            continue  # já foi processada no loop SAN→PDF ou é inválida

        # Se a unidade está VENDIDA no PDF e não existe no SAN, não precisa cadastrar
        _st = normalize_status(row_c[col_c_status]) if col_c_status else "desconhecido"
        # Override universal: qualquer célula da linha diz "VENDIDA"/"VENDIDO"
        if _st == "desconhecido" and _row_is_vendida(row_c):
            _st = "vendido"
        # Fallback: coluna de status detectada mas vazia + sem preço → vendida
        if _st == "desconhecido" and col_c_status and col_c_valor:
            if parse_money(row_c.get(col_c_valor, "")) is None:
                _st = "vendido"
        if _st in ("vendido", "reservado"):
            continue  # vendida e não está no SAN — ignorar, não precisa cadastrar

        valor_c = parse_money(row_c[col_c_valor])
        if unid_pdf in unids_san_todos:
            # Existe no SAN, mas não é nossa captação
            resultados.append({
                "Unidade": str(row_c[col_c_unid]), "COD SAN": "—",
                "Ação": "⬜ NO SAN SEM CAPTACAO",
                "Motivo": "Unidade esta no SAN mas nao como 'Imovel da associada' — verificar",
                "Captacao": "—",
                "Valor SAN": "—", "Valor Construtora": fmt_money(valor_c),
                "_cod": None, "_novo_valor": None,
            })
        else:
            # Genuinamente ausente do SAN
            resultados.append({
                "Unidade": str(row_c[col_c_unid]), "COD SAN": "—",
                "Ação": "🔵 NO PDF MAS NAO NO SAN",
                "Motivo": "Esta na tabela da construtora mas nao cadastrada no SAN",
                "Captacao": "—",
                "Valor SAN": "—", "Valor Construtora": fmt_money(valor_c),
                "_cod": None, "_novo_valor": None,
            })

    return pd.DataFrame(resultados)

def run_bot(config, acoes_file=None):
    config_file = tempfile.mktemp(suffix=".json")
    result_file = tempfile.mktemp(suffix=".json")
    with open(config_file, "w", encoding="utf-8") as f:
        json.dump(config, f)

    cmd = ["python", BOT, "--config", config_file, "--output", result_file]
    if acoes_file:
        cmd += ["--acoes", acoes_file]

    log_box   = st.empty()
    log_lines = []
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace")
    for line in proc.stdout:
        log_lines.append(line.rstrip())
        log_box.code("\n".join(log_lines[-16:]))
    proc.wait()
    try:
        os.unlink(config_file)
    except Exception:
        pass
    return proc.returncode, log_lines, result_file

# ── sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.header("🔐 Login SAN")
    username = st.text_input("Usuário (e-mail)")
    password = st.text_input("Senha", type="password")
    san_url  = st.text_input("URL do empreendimento",
                              placeholder="https://san.redenetimoveis.com/San/Imovel/resultadoimoveis.aspx?...")
    st.caption("Abra o empreendimento no SAN, copie a URL e cole aqui.")

# ── PDF ───────────────────────────────────────────────────────────────────────

st.subheader("📄 Tabela da Construtora (PDF)")
pdf_file = st.file_uploader("Suba o PDF", type=["pdf"])

df_construtora = None
col_c_unid = col_c_status = col_c_valor = None
sem_status = False

if pdf_file:
    with st.spinner("Lendo PDF..."):
        df_construtora, err = extract_pdf(pdf_file)
    if err:
        st.error(err)
    else:
        st.success(f"{len(df_construtora)} linhas extraídas")
        st.dataframe(df_construtora, use_container_width=True, height=220)
        cols_c = df_construtora.columns.tolist()

        # Auto-detecta se o PDF tem coluna de status (sem checkbox — evita cache do Streamlit)
        # "dispon" pega "disponível" (í acentuado) e "disponivel"; "vendid" pega vendido/vendida
        status_re_auto = re.compile(r"(?i)(vendid|[aà]\s*venda|dispon|promo|inativ|reserv)")
        _best_status_col = None
        for _col in cols_c:
            if df_construtora[_col].apply(
                    lambda v: bool(status_re_auto.search(str(v) if v is not None else ""))).sum() >= 1:
                _best_status_col = _col
                break
        sem_status = (_best_status_col is None)

        if sem_status:
            st.info("PDF sem coluna de status detectada — unidades ausentes no PDF serão marcadas REMOVER.")
        else:
            st.info(f"Coluna de status detectada: **{_best_status_col}** (VENDIDO/À VENDA). "
                    f"Unidades com status 'VENDIDO' serão marcadas REMOVER.")

        c1, c2, c3 = st.columns(3)
        col_c_unid  = c1.selectbox("Coluna → Unidade", cols_c,
            index=cols_c.index(detect_col(cols_c, ["unid", "apto", "ap.", "lote", "bloco", "und"])))

        if _best_status_col:
            col_c_status = c2.selectbox("Coluna → Status", cols_c,
                index=cols_c.index(_best_status_col))
        else:
            c2.markdown("*Status: não detectado no PDF*")
            col_c_status = None

        # Auto-detecta coluna de valor pelo conteúdo (R$ ou número grande)
        default_valor = detect_valor_col(df_construtora)
        col_c_valor = c3.selectbox("Coluna → Valor (Valor total ou similar)", cols_c,
            index=cols_c.index(default_valor))

        st.caption(f"Auto-detecção de valor: **{default_valor}** "
                   f"(verifique se é a coluna correta — ex: 'Valor total')")

st.divider()

# ── botão principal ───────────────────────────────────────────────────────────

pronto = bool(username and password and san_url and df_construtora is not None
              and col_c_unid and col_c_valor)

if st.button("🤖  Ler SAN e comparar", type="primary", disabled=not pronto):
    # Limpa resultados anteriores para não mostrar dados antigos
    for k in ["resultado", "san_config"]:
        st.session_state.pop(k, None)

    st.info("Navegador abrindo... acompanhe o que o bot está fazendo.")
    config = {"url": san_url, "username": username, "password": password}
    rc, logs, result_file = run_bot(config)

    if rc != 0 or not os.path.exists(result_file):
        st.error("Bot encerrou com erro. Verifique o log acima.")
    else:
        with open(result_file, encoding="utf-8") as f:
            san_units = json.load(f)
        try:
            os.unlink(result_file)
        except Exception:
            pass

        if not san_units:
            st.warning("Nenhuma unidade extraída. Verifique login e URL.")
        else:
            df_san = pd.DataFrame(san_units)
            st.success(f"✅ {len(df_san)} unidades lidas do SAN")

            with st.expander("Ver dados brutos do SAN"):
                st.dataframe(df_san, use_container_width=True)

            resultado = compare(df_construtora, col_c_unid, col_c_status, col_c_valor,
                                sem_status, df_san)
            st.session_state["resultado"]   = resultado
            st.session_state["san_config"]  = config

elif not pronto:
    missing = []
    if not username or not password: missing.append("login e senha (painel lateral)")
    if not san_url:                  missing.append("URL do empreendimento (painel lateral)")
    if df_construtora is None:       missing.append("PDF da construtora")
    if missing:
        st.info("Preencha: " + ", ".join(missing) + ".")

# ── resultado ─────────────────────────────────────────────────────────────────

if "resultado" in st.session_state:
    resultado = st.session_state["resultado"]

    remover   = resultado[resultado["Ação"].str.startswith("🔴")]
    atualizar = resultado[resultado["Ação"].str.startswith("🟡")]
    ok        = resultado[resultado["Ação"].str.startswith("🟢")]
    nao_san   = resultado[resultado["Ação"].str.startswith("🔵")]
    sem_capt  = resultado[resultado["Ação"].str.startswith("⬜")]
    repasse   = resultado[resultado["Ação"].str.startswith("⚠️")]
    tipo_dif  = resultado[resultado["Ação"].str.startswith("🟠")]
    sem_unid  = resultado[resultado["Ação"].str.startswith("❓")]

    st.subheader("Resultado da comparação")
    m1, m2, m3, m4, m5, m6, m7, m8 = st.columns(8)
    m1.metric("🔴 Remover",                  len(remover))
    m2.metric("🟡 Atualizar valor",           len(atualizar))
    m3.metric("🟢 OK",                       len(ok))
    m4.metric("🔵 Não no SAN",               len(nao_san))
    m5.metric("⬜ No SAN sem captação",       len(sem_capt))
    m6.metric("⚠️ Repasse / verificar",       len(repasse))
    m7.metric("🟠 Tipo diferente",            len(tipo_dif))
    m8.metric("❓ Sem unidade",               len(sem_unid))

    cols_show = ["Unidade", "COD SAN", "Ação", "Motivo", "Captacao", "Valor SAN", "Valor Construtora"]
    st.dataframe(resultado[cols_show], use_container_width=True, hide_index=True)

    if not nao_san.empty:
        st.subheader("🔵 Não cadastradas no SAN")
        st.caption("Essas unidades estão na tabela da construtora mas não existem no SAN — precisam ser cadastradas.")
        st.dataframe(nao_san[["Unidade", "Valor Construtora", "Motivo"]],
                     use_container_width=True, hide_index=True)

    if not sem_capt.empty:
        st.subheader("🏷️ No SAN — mas não é captação sua")
        st.caption("Essas unidades existem no SAN mas não estão marcadas como 'Imóvel da associada'. "
                   "Verifique se a captação precisa ser criada ou atribuída.")
        # Acrescenta etiqueta visual na coluna Ação para ficar visível na tabela
        sem_capt_show = sem_capt[["Unidade", "Valor Construtora"]].copy()
        sem_capt_show.insert(1, "Etiqueta",
            ["🏷️ NÃO É CAPTAÇÃO"] * len(sem_capt_show))
        st.dataframe(sem_capt_show, use_container_width=True, hide_index=True)

    if not tipo_dif.empty:
        st.subheader("🟠 Tipo diferente — verificar manualmente")
        st.caption("O número de quartos no SAN não bate com a seção do PDF da construtora.")
        st.dataframe(tipo_dif[cols_show], use_container_width=True, hide_index=True)

    if not repasse.empty:
        st.subheader("⚠️ Repasse — verificar manualmente")
        st.caption("Essas unidades são de captação de outra associada. Não serão alteradas automaticamente.")
        st.dataframe(repasse[cols_show], use_container_width=True, hide_index=True)

    # ── executar ações ────────────────────────────────────────────────────────
    acoes_exec = []
    for _, row in remover.iterrows():
        if row.get("_cod"):
            acoes_exec.append({"cod": row["_cod"], "acao": "remover",
                                "observacao": "Atualizado conforme tabela da construtora — unidade vendida/reservada"})
    for _, row in atualizar.iterrows():
        if row.get("_cod") and row.get("_novo_valor"):
            acoes_exec.append({"cod": row["_cod"], "acao": "atualizar_valor",
                                "novo_valor": row["_novo_valor"]})

    if acoes_exec:
        st.divider()
        st.subheader("⚡ Executar alterações no SAN")

        if not remover.empty:
            st.markdown("**🔴 Serão removidas** (Editar status → Inativo):")
            st.dataframe(remover[["Unidade", "COD SAN", "Captacao", "Valor SAN"]],
                         use_container_width=True, hide_index=True)
        if not atualizar.empty:
            st.markdown("**🟡 Terão o valor atualizado:**")
            st.dataframe(atualizar[["Unidade", "COD SAN", "Captacao", "Valor SAN", "Valor Construtora"]],
                         use_container_width=True, hide_index=True)

        st.warning(f"O bot vai executar **{len(acoes_exec)} alteração(ões)** diretamente no SAN. "
                   "Apenas imóveis da associada serão alterados.")

        st.markdown("---")
        st.markdown("**🤖 Análise automática antes de executar:**")

        # Verifica se alguma unidade a remover está marcada como "SEM UNIDADE" (nan)
        alertas = []
        for _, row in remover.iterrows():
            if _safe_str(row.get("Unidade")) in ("", "nan", "None"):
                alertas.append(f"⚠️ COD {row['COD SAN']}: unidade não identificada — remoção bloqueada")
            if not row.get("_cod"):
                alertas.append(f"⚠️ Unidade {row['Unidade']}: sem COD SAN válido — não será executada")

        for _, row in atualizar.iterrows():
            if not row.get("_novo_valor"):
                alertas.append(f"⚠️ Unidade {row['Unidade']}: valor novo ausente — não será atualizado")
            if parse_money(row.get("Valor Construtora")) and parse_money(row.get("Valor SAN")):
                diff = abs(parse_money(row["Valor Construtora"]) - parse_money(row["Valor SAN"]))
                if diff > 50000:
                    alertas.append(f"⚠️ Unidade {row['Unidade']}: diferença de "
                                   f"{fmt_money(diff)} — verifique se está correto")

        if alertas:
            for a in alertas:
                st.warning(a)
            # Remove ações com problemas (sem COD ou sem valor) da execução
            acoes_exec = [a for a in acoes_exec
                          if a.get("cod") and
                          (a["acao"] != "atualizar_valor" or a.get("novo_valor"))]
            st.info(f"Após análise: **{len(acoes_exec)} ação(ões) válidas** para execução.")
        else:
            st.success(f"✅ Análise OK — {len(acoes_exec)} ação(ões) verificadas e prontas.")

        if st.button("✅ Executar no SAN agora", type="primary"):
            acoes_file  = tempfile.mktemp(suffix=".json")
            with open(acoes_file, "w", encoding="utf-8") as f:
                json.dump(acoes_exec, f, ensure_ascii=False, indent=2)

            rc, logs, result_file = run_bot(st.session_state["san_config"], acoes_file)
            try:
                os.unlink(acoes_file)
            except Exception:
                pass

            if os.path.exists(result_file):
                with open(result_file, encoding="utf-8") as f:
                    res = json.load(f)
                try:
                    os.unlink(result_file)
                except Exception:
                    pass
                ok_n  = sum(1 for r in res if r["ok"])
                err_n = len(res) - ok_n
                if err_n == 0:
                    st.success(f"✅ Todas as {ok_n} alterações executadas com sucesso!")
                else:
                    st.warning(f"✅ {ok_n} executadas | ⚠️ {err_n} com erro — veja o log acima.")
            else:
                st.error("Erro ao executar ações.")
    elif "resultado" in st.session_state and ok is not None and len(ok) == len(resultado):
        st.success("Tudo certo! Nenhuma alteração necessária.")
