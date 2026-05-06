"""
Bot SAN — duas funções:
  modo_leitura : entra no SAN, busca o número de unidade de cada card
                 'Imóvel da associada' via página de detalhe (sem depender
                 do AJAX do olhinho) e salva o resultado em JSON.
  modo_acao    : recebe lista de ações e executa cada uma no SAN.
"""
import json, re, time, argparse
from urllib.parse import urlparse


def log(msg):
    print(msg, flush=True)


# ── login ─────────────────────────────────────────────────────────────────────

def fazer_login(page, username, password):
    log("Fazendo login...")
    try:
        for sel in ['input[type="email"]', 'input[name*="ogin"]', 'input[name*="suario"]',
                    'input[id*="ogin"]', 'input[id*="suario"]']:
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.fill(username)
                break

        pwd = page.query_selector('input[type="password"]')
        if pwd:
            pwd.fill(password)

        for sel in ['input[type="submit"]', 'button[type="submit"]',
                    'button:has-text("Entrar")', 'a:has-text("Entrar")']:
            btn = page.query_selector(sel)
            if btn and btn.is_visible():
                btn.click()
                page.wait_for_load_state("networkidle", timeout=20000)
                log("Login concluido.")
                return True

        log("AVISO: complete o login manualmente no navegador.")
        time.sleep(10)
        return True
    except Exception as e:
        log(f"Erro no login: {e}")
        return False


# ── extração de unidade do HTML de qualquer texto/página ─────────────────────

def _extrair_unidade_do_html(html):
    """
    Extrai o número da unidade de um texto HTML.
    Suporta 'Apto 502', 'Complemento 502', 'Rua X, 43 - 502', campos de formulário etc.
    """
    import html as _html_lib

    patterns = [
        r'[Aa]p(?:to?|artamento)\.?\s*[Nn]?[º°.]?\s*(\d{2,4})\b',
        # "Complemento Apto 502" ou "Complemento 502" (página de detalhe)
        r'[Cc]omplemento\s+(?:[Aa]pto?\.?\s*|[Aa][Pp]\.?\s*|[Ss]ala\s*)?(\d{2,4})\b',
        r'[Uu]nidade\s+(\d{2,4})\b',
        r'[Ss]ala\s+(\d{2,4})\b',
        r'[Ll]ote\s+(\d{2,4})\b',
        r'[Cc]asa\s+(\d{2,4})\b',
        r'[Cc]onjunto\s+(\d{2,4})\b',
        r'[Cc]obertura\s+(\d{2,4})\b',
        # "Rua X, 43 - 502" — complemento após traço (qualquer traço Unicode)
        r',\s*\d+\s*[\-‐-―−]\s*(\d{2,4})\b',
    ]

    # Converte TODAS as entidades HTML (&ndash; &#8211; &#x2013; etc.) para Unicode
    text = _html_lib.unescape(html)
    # Remove tags e colapsa espaços → texto plano visível
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text)

    for pat in patterns:
        m = re.search(pat, text)
        if m:
            return m.group(1)

    # Fallback: valor de campo de formulário com nome relacionado a complemento/apto
    for pat in [
        r'(?i)(?:complemento|apto)[^>]*?value=["\'](\d{2,5})["\']',
        r'(?i)value=["\'](\d{2,5})["\'][^>]*?(?:complemento|apto)',
    ]:
        m = re.search(pat, html)
        if m:
            return m.group(1)

    return None


# ── coleta básica de cards (COD, valor, captação, URL de detalhe) ─────────────

def _extrair_cards_basicos(page):
    """
    Lê TODOS os cards da página atual (associada, repasse e outros).
    captacao: 'associada' | 'repasse' | 'outro'
    Retorna lista de dicts: {codigo, valor_raw, captacao, detail_url, quartos}.
    """
    return page.evaluate(r"""() => {
        const results  = [];
        const seenCod  = new Set();

        for (const li of document.querySelectorAll('li[data-imovel]')) {
            const cod = li.getAttribute('data-imovel');
            if (!cod || seenCod.has(cod)) continue;

            // Preço (obrigatório para continuar)
            const valorEl   = li.querySelector('.h2-valor, .valorVendaImovel');
            const valorText = (valorEl?.innerText || '').replace(/[R$\s]/g, '').trim();
            if (!valorText) continue;

            const badgeText   = (li.querySelector('.iconMenu')?.innerText || '').trim();
            const liText      = (li.innerText || '').trim();
            const isAssociada = /associada/i.test(badgeText) || /associada/i.test(liText);
            const isRepasse   = /repasse/i.test(badgeText)   || /repasse/i.test(liText);
            const captacao    = isAssociada ? 'associada' : (isRepasse ? 'repasse' : 'outro');

            // Endereço já visível no card (pode conter a unidade se o olhinho foi clicado)
            const addrEl    = li.querySelector('.endereco-text');
            const addrTexto = addrEl ? addrEl.innerText.trim() : '';

            // URL da página de detalhe
            const aEl       = li.querySelector('a[href*="detalhe_imovel"]');
            const detailUrl = aEl ? aEl.href : null;

            // URL da página de edição (tem todos os campos do formulário preenchidos)
            const editEl  = li.querySelector('a[href*="novoImovel"][href*="acao=2"]:not([href*="fotos"])');
            const editUrl = editEl ? editEl.href : null;

            // Número de quartos
            const ulText  = (li.querySelector('ul')?.innerText || liText);
            const qtosM   = ulText.match(/(\d+)\s*quartos?/i);
            const quartos = qtosM ? parseInt(qtosM[1]) : null;

            seenCod.add(cod);
            results.push({
                codigo    : cod,
                valor_raw : valorText,
                captacao  : captacao,
                detail_url: detailUrl,
                edit_url  : editUrl,
                quartos   : quartos,
                addr_texto: addrTexto,
            });
        }
        return results;
    }""")


# ── tenta clicar no olhinho para revelar endereço completo ────────────────────

def _tentar_olhinho(page, cod):
    """
    Clica no botão 'Visualizar endereço' do card e retorna o texto do endereço
    atualizado. Retorna None se o endereço não mudar ou se o botão não for encontrado.
    """
    try:
        btn = page.locator(
            f'li[data-imovel="{cod}"] button[title*="nder"],'
            f'li[data-imovel="{cod}"] button[data-id="{cod}"]'
        )
        if btn.count() == 0:
            return None

        addr_loc = page.locator(f'li[data-imovel="{cod}"] .endereco-text')
        addr_antes = addr_loc.first.inner_text().strip() if addr_loc.count() > 0 else ''

        btn.first.scroll_into_view_if_needed()
        btn.first.click(timeout=3000)

        # Aguarda o endereço mudar (AJAX atualiza o DOM)
        try:
            page.wait_for_function(
                f"""() => {{
                    const el = document.querySelector('li[data-imovel="{cod}"] .endereco-text');
                    return el && el.innerText.trim().length > 20
                        && el.innerText.trim() !== {repr(addr_antes)};
                }}""",
                timeout=4000,
            )
        except Exception:
            time.sleep(1.5)

        addr_depois = addr_loc.first.inner_text().strip() if addr_loc.count() > 0 else ''
        if addr_depois and addr_depois != addr_antes:
            return addr_depois
        return None
    except Exception as e:
        log(f"  COD {cod}: olhinho falhou: {e}")
        return None


# ── busca a unidade via página de detalhe ─────────────────────────────────────

def _buscar_unidades_via_detalhe(page, cards, origin, save_first_html=None):
    """
    Para cada card, faz um GET autenticado na página de detalhe e extrai
    o número de unidade do HTML retornado.
    """
    results = []
    saved_diag = False

    for card in cards:
        cod        = card['codigo']
        detail_url = card.get('detail_url')

        # Monta URL se o JS não resolveu
        if not detail_url:
            detail_url = f"{origin}/imovel/detalhe_imovel.aspx?imovel_id={cod}"

        try:
            # Tenta primeiro o endereço já visível no card (antes de buscar detalhe)
            addr_card = card.get('addr_texto', '')
            unidade_rapida = _extrair_unidade_do_html(addr_card) if addr_card else None
            if unidade_rapida:
                log(f"  COD {cod}: unidade '{unidade_rapida}' extraida do card (sem fetch)")
                results.append({
                    'codigo'     : cod,
                    'unidade'    : unidade_rapida,
                    'torre'      : None,
                    'bloco'      : None,
                    'id_completo': unidade_rapida,
                    'valor_raw'  : card['valor_raw'],
                    'captacao'   : card['captacao'],
                    'quartos'    : card.get('quartos'),
                })
                time.sleep(0.1)
                continue

            # Tenta clicar no olhinho para revelar endereço completo
            addr_olhinho = _tentar_olhinho(page, cod)
            if addr_olhinho:
                unidade_olhinho = _extrair_unidade_do_html(addr_olhinho)
                if unidade_olhinho:
                    log(f"  COD {cod}: unidade '{unidade_olhinho}' via olhinho: '{addr_olhinho}'")
                    results.append({
                        'codigo'     : cod,
                        'unidade'    : unidade_olhinho,
                        'torre'      : None,
                        'bloco'      : None,
                        'id_completo': unidade_olhinho,
                        'valor_raw'  : card['valor_raw'],
                        'captacao'   : card['captacao'],
                        'quartos'    : card.get('quartos'),
                    })
                    time.sleep(0.1)
                    continue

            resp = page.request.get(detail_url, timeout=20000)
            html = resp.text()

            # Diagnóstico: mostra contexto do complemento na página de detalhe
            import html as _hl
            _txt = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', _hl.unescape(html)))
            for _kw in ['complemento', 'joaquim']:
                _idx = _txt.lower().find(_kw)
                if _idx >= 0:
                    log(f"  COD {cod} detalhe [{_kw}]: ...{_txt[max(0,_idx-10):_idx+80]}...")
                    break

            # Salva HTML do primeiro card para diagnóstico
            if save_first_html and not saved_diag:
                import os as _os2
                diag_file = _os2.path.join(_os2.path.dirname(save_first_html), "diag_detalhe.html")
                with open(diag_file, "w", encoding="utf-8") as f:
                    f.write(html)
                log(f"  DIAG: pagina de detalhe salva em {diag_file}")
                saved_diag = True

            unidade = _extrair_unidade_do_html(html)

            # Se não achou na página de detalhe, salva para diagnóstico e tenta edição
            if unidade is None:
                import os as _os3
                diag_det = _os3.path.join(_os3.path.dirname(__file__),
                                          f"diag_detalhe_{cod}.html")
                with open(diag_det, "w", encoding="utf-8") as f:
                    f.write(html)
                log(f"  COD {cod}: unidade nao achou no detalhe. Salvo em {diag_det}")

                edit_url = card.get('edit_url')
                if not edit_url:
                    edit_url = f"{origin}/Imovel/novoImovel.aspx?acao=2&im={cod}"
                try:
                    resp_edit = page.request.get(edit_url, timeout=20000)
                    html_edit = resp_edit.text()
                    unidade   = _extrair_unidade_do_html(html_edit)
                    if unidade:
                        log(f"  COD {cod}: unidade encontrada na pagina de edicao: {unidade}")
                    else:
                        log(f"  COD {cod}: unidade NAO encontrada em nenhuma pagina")
                except Exception as e2:
                    log(f"  COD {cod}: erro ao buscar pagina de edicao: {e2}")

            m_torre = re.search(r'[Tt]orre\s+(\w+)', html)
            m_bloco = re.search(r'[Bb]loco\s+(\w+)', html)
            torre   = m_torre.group(1) if m_torre else None
            bloco   = m_bloco.group(1) if m_bloco else None

            parts = [unidade,
                     f'T{torre}' if torre else None,
                     f'Bl{bloco}' if bloco else None]
            parts = [p for p in parts if p]

            results.append({
                'codigo'     : cod,
                'unidade'    : unidade,
                'torre'      : torre,
                'bloco'      : bloco,
                'id_completo': ' '.join(parts) if parts else cod,
                'valor_raw'  : card['valor_raw'],
                'captacao'   : card['captacao'],
                'quartos'    : card.get('quartos'),
            })
            log(f"  COD {cod}: unidade={unidade or 'NAO_ENCONTRADA'} | valor={card['valor_raw']}"
                f" | quartos={card.get('quartos')}")
            time.sleep(0.3)

        except Exception as e:
            log(f"  COD {cod}: erro ao buscar detalhe: {e}")
            results.append({
                'codigo'     : cod,
                'unidade'    : None,
                'torre'      : None,
                'bloco'      : None,
                'id_completo': cod,
                'valor_raw'  : card['valor_raw'],
                'captacao'   : card['captacao'],
                'quartos'    : card.get('quartos'),
            })

    return results


# ── extração completa de unidades da página atual ─────────────────────────────

def extrair_unidades(page, origin=None, save_first_html=None):
    """
    Extrai todos os cards 'Imóvel da associada' da página atual,
    buscando o número de unidade na página de detalhe de cada card.
    """
    cards = _extrair_cards_basicos(page)
    if not cards:
        return []

    log(f"  {len(cards)} card(s) 'associada' encontrado(s). Buscando unidades...")

    if not origin:
        parsed = urlparse(page.url)
        origin = f"{parsed.scheme}://{parsed.netloc}"

    return _buscar_unidades_via_detalhe(page, cards, origin, save_first_html)


# ── paginação ─────────────────────────────────────────────────────────────────

def _clicar_proxima_pagina(page, current_num):
    next_num = current_num + 1

    container_sels = [
        ".pagination",
        "ul.pagination",
        "[class*='pagination']",
        "[class*='paginac']",
        "[class*='pager']",
        "ul:has(a.page-link)",
        "ul:has(.page-link)",
    ]

    for c_sel in container_sels:
        try:
            container = page.locator(c_sel)
            if container.count() == 0:
                continue

            for btn_sel in [
                f'a:text-is("{next_num}")',
                f'a.page-link:text-is("{next_num}")',
                f'li:not(.active):not(.disabled) a:text-is("{next_num}")',
            ]:
                try:
                    btn = container.locator(btn_sel)
                    if btn.count() > 0:
                        btn.first.click()
                        page.wait_for_load_state("networkidle", timeout=20000)
                        time.sleep(2)
                        log(f"  -> Pagina {next_num}: clicou '{next_num}' em '{c_sel}'")
                        return True
                except Exception:
                    pass

            for btn_sel in [
                'a:text-is("Próxima")', 'a:text-is("Proxima")',
                'a.page-link:text-is("Próxima")', 'a.page-link:text-is("Proxima")',
                'a.page-link.next', 'a.page-link[rel="next"]',
                'li:not(.disabled) a:text-is("Próxima")',
                'li:not(.disabled) a:text-is("Proxima")',
                'a:text-is("»")', 'a:text-is("Next")',
            ]:
                try:
                    btn = container.locator(btn_sel)
                    if btn.count() > 0:
                        btn.first.click()
                        page.wait_for_load_state("networkidle", timeout=20000)
                        time.sleep(2)
                        log(f"  -> Pagina {next_num}: clicou 'Proxima' em '{c_sel}'")
                        return True
                except Exception:
                    pass

        except Exception:
            pass

    diag = page.evaluate(r"""() => {
        return [...document.querySelectorAll('a')]
            .filter(a => {
                const t = a.innerText.trim();
                return /^\d+$/.test(t) || t === 'Anterior' || t === 'Próxima' || t === 'Proxima';
            })
            .map(a => ({
                text: a.innerText.trim(),
                cls: a.className,
                pTag: (a.parentElement||{}).tagName||'',
                pCls: (a.parentElement||{}).className||'',
                href: a.getAttribute('href')||'',
            }));
    }""")
    log(f"  Paginacao: container nao encontrado. Links candidatos:")
    for item in (diag or [])[:15]:
        log(f"    text='{item.get('text')}' cls='{item.get('cls')}'"
            f" pTag='{item.get('pTag')}' pCls='{item.get('pCls')}'")
    return False


# ── coleta de todas as páginas ────────────────────────────────────────────────

def coletar_todas_paginas(page, base_url):
    all_units = []
    seen_cods = set()

    url_p1 = re.sub(r"[?&]pa=\d+", "", base_url)
    url_p1 = re.sub(r"[?&]page=\d+", "", url_p1)
    url_p1 = re.sub(r"#.*", "", url_p1)
    page.goto(url_p1, timeout=30000)
    page.wait_for_load_state("networkidle", timeout=30000)
    time.sleep(2)
    log(f"Pagina carregada: {page.title()[:60]} | URL: ...{page.url[-50:]}")

    parsed = urlparse(page.url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    import os as _os
    diag_path = _os.path.join(_os.path.dirname(__file__), "diag_eye.html")

    page_num = 1
    while True:
        log(f"--- Pagina {page_num} ---")

        units = extrair_unidades(
            page,
            origin=origin,
            save_first_html=diag_path if page_num == 1 else None,
        )

        if not units:
            log(f"Pagina {page_num}: sem cards. Encerrando.")
            break

        new_units = [u for u in units if u["codigo"] not in seen_cods]
        log(f"  {len(new_units)} novas unidades (de {len(units)} nesta pagina).")
        seen_cods.update(u["codigo"] for u in units)
        all_units.extend(new_units)

        if not _clicar_proxima_pagina(page, page_num):
            log(f"Pagina {page_num}: ultima pagina.")
            break
        page_num += 1

    log(f"TOTAL: {len(all_units)} unidades extraidas.")
    return all_units


# ── clicar nos três pontos de um card específico ──────────────────────────────

def clicar_tres_pontos(page, cod):
    try:
        resultado = page.evaluate(f"""() => {{
            const li = document.querySelector('li[data-imovel="{cod}"]');
            if (!li) return 'card_nao_encontrado';

            // Botão dos três pontos (⋮) — classe específica do SAN
            const btn = li.querySelector(
                '.btn-ferramenta, button.dropdown-toggle[data-toggle="dropdown"], '
                + 'button[data-bs-toggle="dropdown"], button.dropdown-toggle'
            );
            if (btn) {{ btn.click(); return 'ok'; }}

            // Fallback: último botão dentro do card
            const btns = li.querySelectorAll('button');
            if (btns.length) {{ btns[btns.length - 1].click(); return 'ok_fallback'; }}

            return 'botao_nao_encontrado';
        }}""")
        log(f"  Tres pontos COD {cod}: {resultado}")
        return resultado.startswith("ok")
    except Exception as e:
        log(f"  Erro ao clicar tres pontos: {e}")
        return False


# ── ações individuais ─────────────────────────────────────────────────────────

def _clicar_item_menu(page, cod, texto_item, classe_item=None):
    """
    Clica no item do menu de ferramentas do card sem precisar abrir o dropdown.
    Tenta: 1) por classe CSS, 2) por texto, 3) abre dropdown e clica por texto.
    """
    resultado = page.evaluate(f"""() => {{
        const li = document.querySelector('li[data-imovel="{cod}"]');
        if (!li) return 'card_nao_encontrado';

        // Tenta por classe específica (ex: .funcaoAlteraValorImovel)
        {f'const byClass = li.querySelector("{classe_item}"); if (byClass) {{ byClass.click(); return "ok_class"; }}' if classe_item else ''}

        // Tenta por texto dentro do dropdown (funciona mesmo com display:none)
        for (const el of li.querySelectorAll('.dropFerramentas li, .dropdown-menu li')) {{
            if (/{texto_item}/i.test(el.innerText)) {{
                el.click();
                return 'ok_text';
            }}
        }}
        return 'nao_encontrado';
    }}""")
    log(f"  Menu '{texto_item}' COD {cod}: {resultado}")
    return resultado.startswith("ok")


def atualizar_valor(page, cod, novo_valor_str):
    if not _clicar_item_menu(page, cod, "Editar valor", ".funcaoAlteraValorImovel"):
        if clicar_tres_pontos(page, cod):
            time.sleep(0.8)
            if not _clicar_item_menu(page, cod, "Editar valor", ".funcaoAlteraValorImovel"):
                return False
        else:
            return False
    try:
        try:
            page.wait_for_load_state("networkidle", timeout=4000)
        except Exception:
            pass
        page.wait_for_selector("text=ALTERAR VALOR DO IMÓVEL", state="visible", timeout=8000)
        time.sleep(1.5)

        # Descobre o ID do input via JavaScript
        inp_id = page.evaluate("""() => {
            let modal = document.querySelector('.modal.show')
                     || document.querySelector('.modal.in');
            if (!modal) {
                for (const el of document.querySelectorAll('.modal, [class*="modal"]')) {
                    if (el.offsetParent !== null && el.innerText &&
                            el.innerText.includes('ALTERAR VALOR')) {
                        modal = el; break;
                    }
                }
            }
            if (!modal) return null;
            const inp = modal.querySelector('input[type="text"]');
            return inp ? (inp.id || inp.name || null) : null;
        }""")
        log(f"  COD {cod}: input id = {inp_id}")

        if not inp_id:
            log(f"  COD {cod}: campo Valor da Venda nao encontrado no modal")
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            return False

        # Salva fonte de GravaValorEmpreendimento para diagnóstico
        try:
            func_src = page.evaluate("""() =>
                typeof GravaValorEmpreendimento === 'function'
                    ? GravaValorEmpreendimento.toString().slice(0, 2000)
                    : 'not_found'
            """)
            import os as _os_d
            diag_js = _os_d.path.join(_os_d.path.dirname(__file__), "diag_grava_func.txt")
            with open(diag_js, "w", encoding="utf-8") as _f:
                _f.write(func_src)
            log(f"  DIAG func (150 chars): {func_src[:150]}")
        except Exception as _e:
            log(f"  DIAG func erro: {_e}")

        valor_so_digitos = re.sub(r'[^\d]', '', novo_valor_str)
        log(f"  COD {cod}: digitos = '{valor_so_digitos}'")

        # ── Passo 1: limpa via jQuery (reseta estado interno da máscara) ──────
        clear_result = page.evaluate(f"""() => {{
            const el = document.getElementById('{inp_id}');
            if (!el) return 'no_el';
            const before = el.value;
            el.focus();
            if (window.jQuery) {{
                jQuery(el).val('');
                jQuery(el).trigger('input').trigger('change').trigger('keyup');
            }} else {{
                el.value = '';
                ['input','change','keyup'].forEach(
                    ev => el.dispatchEvent(new Event(ev, {{bubbles:true}}))
                );
            }}
            return 'ok|was=' + before + '|now=' + el.value;
        }}""")
        log(f"  COD {cod}: clear jQuery = {clear_result}")
        time.sleep(0.3)

        # ── Passo 2: clica no campo e digita os dígitos
        # (a máscara de dinheiro preenche da direita pra esquerda automaticamente)
        inp_loc = page.locator(f'#{inp_id}')
        inp_loc.click(timeout=3000)
        time.sleep(0.2)
        page.keyboard.type(valor_so_digitos, delay=80)
        time.sleep(0.5)

        # ── Diagnóstico: lê o valor atual em todas as fontes ─────────────────
        val_check = page.evaluate(f"""() => {{
            const el = document.getElementById('{inp_id}');
            if (!el) return 'no_el';
            const jqVal = window.jQuery ? jQuery(el).val() : 'no_jq';
            const allInps = [...document.querySelectorAll('.modal.show input, .modal.in input')]
                .map(i => ({{id:i.id, type:i.type, val:i.value}}) );
            return {{domVal: el.value, jqVal: jqVal, allInps: allInps}};
        }}""")
        log(f"  COD {cod}: val_check = {val_check}")

        # ── Passo 3: submete — tenta GravaValorEmpreendimento primeiro ────────
        r_grava = page.evaluate("""() => {
            if (typeof GravaValorEmpreendimento === 'function') {
                GravaValorEmpreendimento();
                return 'ok_grava';
            }
            return 'nao_encontrada';
        }""")
        log(f"  COD {cod}: grava = {r_grava}")

        # ── Passo 4: se função não existe, clica o botão fisicamente ─────────
        if r_grava != 'ok_grava':
            clicou_btn = False
            for sel in [
                '.modal.show button:has-text("ALTERAR VALOR")',
                '.modal.in button:has-text("ALTERAR VALOR")',
                '.modal.show button:has-text("ALTERAR")',
                '.modal.in button:has-text("ALTERAR")',
            ]:
                btn_loc = page.locator(sel)
                if btn_loc.count() > 0:
                    btn_loc.first.click(timeout=5000)
                    log(f"  COD {cod}: botao fisico '{sel}' clicado")
                    clicou_btn = True
                    break
            if not clicou_btn:
                log(f"  COD {cod}: ERRO — nem funcao nem botao encontrado")
                return False

        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        time.sleep(0.5)

        # ── Fecha modal de confirmação "valor alterado com sucesso" ───────────
        try:
            page.wait_for_selector(
                '#modalConfirmacaoAlteraValor',
                state='visible',
                timeout=5000,
            )
            fechou = False
            for close_sel in [
                '#modalConfirmacaoAlteraValor [data-dismiss="modal"]',
                '#modalConfirmacaoAlteraValor .close',
                '#modalConfirmacaoAlteraValor button',
            ]:
                close_loc = page.locator(close_sel)
                if close_loc.count() > 0:
                    close_loc.first.click(timeout=3000)
                    fechou = True
                    log(f"  COD {cod}: modal confirmacao fechado")
                    break
            if not fechou:
                page.keyboard.press("Escape")
                log(f"  COD {cod}: modal confirmacao fechado via Escape")
            page.wait_for_selector(
                '#modalConfirmacaoAlteraValor',
                state='hidden',
                timeout=4000,
            )
        except Exception:
            pass

        log(f"  COD {cod}: valor atualizado para {novo_valor_str}")
        return True
    except Exception as e:
        log(f"  Erro ao atualizar valor COD {cod}: {e}")
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return False


def remover_unidade(page, cod, observacao):
    if not _clicar_item_menu(page, cod, "Editar status"):
        if clicar_tres_pontos(page, cod):
            time.sleep(0.8)
            if not _clicar_item_menu(page, cod, "Editar status"):
                return False
        else:
            return False
    try:
        try:
            page.wait_for_load_state("networkidle", timeout=4000)
        except Exception:
            pass
        page.wait_for_selector("text=ALTERAR STATUS DO IMÓVEL", state="visible", timeout=8000)
        time.sleep(1.0)

        # ── Seleciona "Vendido por terceiros" dentro do modal aberto ─────────
        clicou = False
        for sel in [
            '.modal.in label:has-text("Vendido por terceiros")',
            '.modal.show label:has-text("Vendido por terceiros")',
            'label:has-text("Vendido por terceiros")',
        ]:
            loc = page.locator(sel)
            if loc.count() > 0:
                loc.first.click(timeout=3000)
                clicou = True
                log(f"  COD {cod}: 'Vendido por terceiros' selecionado")
                break

        if not clicou:
            for val in ['3', 'VT', 'VendidoTerceiros', 'vendidoterceiros']:
                for prefix in ['.modal.in ', '.modal.show ', '']:
                    loc = page.locator(f'{prefix}input[type="radio"][value="{val}"]')
                    if loc.count() > 0:
                        loc.first.click(timeout=3000)
                        clicou = True
                        break
                if clicou:
                    break

        if not clicou:
            log(f"  COD {cod}: radio 'Vendido por terceiros' nao encontrado")

        time.sleep(0.5)

        # ── Preenche textarea de observação ──────────────────────────────────
        ta_loc = page.locator('.modal.in textarea, .modal.show textarea')
        preencheu = False
        if ta_loc.count() > 0:
            try:
                ta_loc.first.fill("vendido")  # fill() limpa + digita + dispara eventos
                preencheu = True
                log(f"  COD {cod}: textarea preenchida via fill()")
            except Exception as e_ta:
                log(f"  COD {cod}: erro fill textarea: {e_ta}")

        if not preencheu:
            page.evaluate("""() => {
                const modal = document.querySelector('.modal.in') || document.querySelector('.modal.show');
                const ta = modal ? modal.querySelector('textarea') : null;
                if (ta) {
                    ta.focus(); ta.value = 'vendido';
                    ['input','change'].forEach(e => ta.dispatchEvent(new Event(e, {bubbles:true})));
                }
            }""")
            log(f"  COD {cod}: textarea preenchida via JS fallback")

        time.sleep(0.3)

        # ── Diagnóstico estado do formulário antes de submeter ────────────────
        state = page.evaluate("""() => {
            const modal = document.querySelector('.modal.in') || document.querySelector('.modal.show');
            if (!modal) return {err: 'no_modal'};
            const sel = [...modal.querySelectorAll('input[type="radio"]')].find(r => r.checked);
            const ta  = modal.querySelector('textarea');
            const all = [...modal.querySelectorAll('button,a,[onclick],input[type=submit]')];
            return {
                radio: sel ? sel.value : null,
                textarea: ta ? ta.value : null,
                btns: all.map(e => e.tagName + ':' + (e.innerText||e.value||'').trim().slice(0,20)
                             + ' [onclick=' + (e.getAttribute('onclick')||'').slice(0,80) + ']')
            };
        }""")
        log(f"  COD {cod}: state = {state}")

        # ── Se radio perdeu seleção, re-seleciona ────────────────────────────
        if state and not state.get('radio'):
            log(f"  COD {cod}: radio perdido — re-selecionando")
            for sel_label in ['.modal.in label:has-text("Vendido por terceiros")',
                               '.modal.show label:has-text("Vendido por terceiros")']:
                loc = page.locator(sel_label)
                if loc.count() > 0:
                    loc.first.click()
                    time.sleep(0.3)
                    break

        # ── Clica ALTERAR via onclick direto ou click ─────────────────────────
        r_alterar = page.evaluate("""() => {
            const modal = document.querySelector('.modal.in') || document.querySelector('.modal.show');
            if (!modal) return 'no_modal';
            const all = [...modal.querySelectorAll('button,a,[onclick],input[type=submit]')];
            const btn = all.find(el => {
                const txt = (el.innerText || el.value || '').trim().toUpperCase();
                return txt === 'ALTERAR' || txt.startsWith('ALTERAR');
            });
            if (!btn) return 'not_found';
            const oc = btn.getAttribute('onclick') || '';
            try {
                if (oc) { eval(oc); return 'ok_eval:' + oc.slice(0,80); }
                btn.click();
                return 'ok_click';
            } catch(e) {
                btn.click();
                return 'ok_fallback:' + e.message;
            }
        }""")
        log(f"  COD {cod}: ALTERAR = {r_alterar}")

        # ── Aguarda o modal de STATUS fechar (até 10s) ───────────────────────
        sucesso = False
        try:
            page.wait_for_function(
                """() => {
                    const modais = [...document.querySelectorAll('.modal.in, .modal.show')];
                    return !modais.some(m =>
                        getComputedStyle(m).display !== 'none' &&
                        m.getAttribute('aria-hidden') !== 'true' &&
                        (m.innerText || '').includes('ALTERAR STATUS')
                    );
                }""",
                timeout=10000,
            )
            sucesso = True
            log(f"  COD {cod}: modal status fechou — salvo com sucesso")
        except Exception:
            log(f"  COD {cod}: ERRO — modal status nao fechou em 10s")
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            return False

        # ── Fecha modal de confirmação de sucesso ────────────────────────────
        try:
            for close_sel in [
                '.modal.in [data-dismiss="modal"]',
                '.modal.show [data-dismiss="modal"]',
                '.modal.in .close',
                '.modal.show .close',
            ]:
                close_loc = page.locator(close_sel)
                if close_loc.count() > 0:
                    close_loc.first.click(timeout=3000)
                    log(f"  COD {cod}: modal confirmacao fechado")
                    break
        except Exception:
            pass

        log(f"  COD {cod}: unidade marcada como vendida por terceiros")
        return True
    except Exception as e:
        log(f"  Erro ao remover COD {cod}: {e}")
        return False


# ── modos ─────────────────────────────────────────────────────────────────────

def modo_leitura(url, username, password, output_file):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=200)
        page    = browser.new_page()

        log("Abrindo SAN...")
        page.goto("https://san.redenetimoveis.com", timeout=30000)
        page.wait_for_load_state("domcontentloaded")

        if "login" in page.url.lower() or page.query_selector('input[type="password"]'):
            fazer_login(page, username, password)
            time.sleep(2)
        else:
            log("Sessao ja ativa.")

        cards = coletar_todas_paginas(page, url)
        browser.close()

        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(cards, f, ensure_ascii=False, indent=2)


def modo_acao(url, username, password, acoes_file, output_file):
    from playwright.sync_api import sync_playwright

    with open(acoes_file, encoding="utf-8") as f:
        acoes = json.load(f)

    resultados = []
    acoes_dict = {a["cod"]: a for a in acoes}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=400)
        page    = browser.new_page()

        log("Abrindo SAN para executar acoes...")
        page.goto("https://san.redenetimoveis.com", timeout=30000)
        page.wait_for_load_state("domcontentloaded")

        if "login" in page.url.lower() or page.query_selector('input[type="password"]'):
            fazer_login(page, username, password)
            time.sleep(2)

        url_p1 = re.sub(r"[?&]pa=\d+", "", url)
        url_p1 = re.sub(r"[?&]page=\d+", "", url_p1)
        url_p1 = re.sub(r"#.*", "", url_p1)
        page.goto(url_p1, timeout=30000)
        page.wait_for_load_state("networkidle", timeout=30000)
        time.sleep(2)

        page_num  = 1
        seen_cods = set()

        while acoes_dict:
            log(f"--- Pagina {page_num} (acoes restantes: {len(acoes_dict)}) ---")

            # Salva URL completa desta página (incluindo hash #&page=N do SAN)
            # para recarregar exatamente aqui após cada ação
            url_pagina_atual = page.url

            cards     = _extrair_cards_basicos(page)
            new_cards = [c for c in cards if c["codigo"] not in seen_cods]

            if not new_cards:
                log(f"Pagina {page_num}: sem cards novos. Encerrando.")
                break
            seen_cods.update(c["codigo"] for c in new_cards)
            cods_page = {c["codigo"] for c in new_cards}

            # Pré-computa quais ações executar nesta página (evita iteração com URL derivada)
            matches = [cod for cod in list(acoes_dict.keys()) if cod in cods_page]
            log(f"  CODs nesta pagina ({len(cods_page)}): {sorted(cods_page)}")
            log(f"  CODs com acao: {sorted(acoes_dict.keys())}")
            log(f"  Matches: {matches}")

            for cod in matches:
                if cod not in acoes_dict:
                    continue  # já removido em iteração anterior
                acao = acoes_dict[cod]
                log(f"Executando '{acao['acao']}' no COD {cod}...")

                if acao["acao"] == "atualizar_valor":
                    ok = atualizar_valor(page, cod, acao.get("novo_valor", ""))
                elif acao["acao"] == "remover":
                    ok = remover_unidade(page, cod,
                             acao.get("observacao", "Atualizado conforme tabela da construtora"))
                else:
                    ok = False

                resultados.append({"cod": cod, "acao": acao["acao"], "ok": ok})
                del acoes_dict[cod]

                # Recarrega para a URL estável desta página (ignora drift de hash/paginação)
                page.goto(url_pagina_atual, timeout=30000)
                page.wait_for_load_state("networkidle", timeout=20000)
                time.sleep(1.5)

            if not acoes_dict:
                break

            if not _clicar_proxima_pagina(page, page_num):
                break
            page_num += 1

        for cod in acoes_dict:
            resultados.append({"cod": cod, "acao": acoes_dict[cod]["acao"], "ok": False})

        browser.close()

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(resultados, f, ensure_ascii=False, indent=2)
    log("Acoes concluidas.")


# ── entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config",  required=True)
    parser.add_argument("--output",  required=True)
    parser.add_argument("--acoes",   default=None)
    args = parser.parse_args()

    with open(args.config, encoding="utf-8") as f:
        cfg = json.load(f)

    if args.acoes:
        modo_acao(cfg["url"], cfg["username"], cfg["password"], args.acoes, args.output)
    else:
        modo_leitura(cfg["url"], cfg["username"], cfg["password"], args.output)
