#!/usr/bin/env python3
"""Coletor incremental do Portal da Transparência de Teixeira de Freitas.

Requer Python 3.10+ e Playwright (pip install playwright; playwright install chromium).

O coletor navega pela interface pública do portal. O portal usa componentes
Webrun/SmartClient e seus identificadores internos podem mudar. Por isso, esta
versão falha com diagnóstico quando não reconhece um controle; não tenta
contornar CAPTCHA nem autenticação.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from playwright.async_api import async_playwright, Page, Frame, TimeoutError as PlaywrightTimeout

HOME = ("https://portaldatransparencia.sistemasonline.net.br/webrun5/form.jsp"
        "?sys=PTP&action=openform&formID=20&dataConnection=PM_Teixeira_de_Freitas&numerotc=386")
CONTRACTS_URL = ("https://portaldatransparencia.sistemasonline.net.br/webrun5/form.jsp"
                 "?sys=PTP&action=openform&formID=8522&dataConnection=PM_Teixeira_de_Freitas&numerotc=386")
OUT = Path("dados/contratos.json")
STATE = Path("dados/checkpoint.json")
DAY_WINDOW = 10  # tamanho padrão; pode ser alterado com --window-days
OVERLAP_DAYS = 20  # reconsulta a cauda para capturar correções e novos registros retroativos
SCRIPT_VERSION = "2026-10-21-recuperacao-de-linhas"
IGNORED_ORGAO = "CAMARA MUNICIPAL DE TEIXEIRA DE FREITAS"
DIAGNOSTICS_DIR = Path(__file__).resolve().parent / "diagnosticos"


def normalized_orgao(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join(text.upper().split())


def exclude_ignored_orgao(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    eligible = [row for row in rows
                if normalized_orgao(row.get("orgao")) != IGNORED_ORGAO]
    return eligible, len(rows) - len(eligible)


def windows(start: date, end: date, days: int = DAY_WINDOW):
    cursor = start
    while cursor <= end:
        last = min(cursor + timedelta(days=days - 1), end)
        yield cursor, last
        cursor = last + timedelta(days=1)


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("informe um número inteiro de dias") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("o tamanho da janela deve ser maior que zero")
    return parsed


def br_date(d: date) -> str:
    return d.strftime("%d/%m/%Y")


def iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"data inválida: {value!r}. Use AAAA-MM-DD, por exemplo 2021-01-01."
        ) from exc


async def web_frame(page: Page, require_contracts: bool = False) -> Frame:
    """Aguarda um frame de conteúdo; detalhes nem sempre contêm o título CONTRATOS."""
    for _ in range(60):
        for frame in page.frames:
            try:
                body = frame.locator("body")
                if await body.count() == 0:
                    continue
                txt = (await body.inner_text(timeout=800)).strip()
                if not txt:
                    continue
                upper = txt.upper()
                if require_contracts:
                    if "CONTRATOS" in upper and "REGISTRO" in upper:
                        return frame
                elif ("PORTAL-TRANSPARENCIA" in upper or "CONTRATO" in upper
                      or "CONTRATADO" in upper or await frame.locator("input").count() > 0):
                    return frame
            except Exception:
                pass
        await page.wait_for_timeout(500)
    details = []
    for frame in page.frames:
        try:
            details.append({"url": frame.url, "title": await frame.locator("title").inner_text(timeout=300) if await frame.locator("title").count() else "", "body": (await frame.locator("body").inner_text(timeout=300))[:300]})
        except Exception:
            pass
    raise RuntimeError(f"Não localizei o conteúdo do portal na guia {page.url}. Frames: {details}")



async def save_portal_access_diagnostic(page: Page, response: Any = None,
                                        error: str | None = None) -> Path:
    """Registra sinais observáveis do bloqueio/carregamento sem guardar dados da grade."""
    headers: dict[str, str] = {}
    status = None
    response_url = None
    if response is not None:
        try:
            status = response.status
            response_url = response.url
            all_headers = await response.all_headers()
            for name in ("server", "cf-ray", "cf-mitigated", "cf-cache-status",
                         "content-type", "location", "retry-after"):
                if all_headers.get(name):
                    headers[name] = all_headers[name]
        except Exception as exc:
            headers["diagnostico_resposta"] = f"não foi possível ler cabeçalhos: {exc}"

    try:
        title = await page.title()
    except Exception:
        title = ""
    try:
        body = (await page.locator("body").inner_text(timeout=2500)).strip()[:1800]
    except Exception:
        body = ""

    try:
        content = (title + "\n" + body).casefold()
        frame_urls = [frame.url for frame in page.frames]
    except Exception:
        content = title.casefold()
        frame_urls = []

    cloudflare_signals = []
    if "cloudflare" in content or "attention required" in content:
        cloudflare_signals.append("título ou conteúdo identifica Cloudflare")
    if headers.get("cf-ray"):
        cloudflare_signals.append("cabeçalho cf-ray presente")
    if headers.get("cf-mitigated", "").lower() == "challenge":
        cloudflare_signals.append("cf-mitigated: challenge")
    if "you have been blocked" in content or "security solution" in content:
        cloudflare_signals.append("página informa bloqueio por solução de segurança")

    if cloudflare_signals:
        classification = "bloqueio/interceptação do Cloudflare antes da grade"
        cause_limit = (
            "O navegador consegue identificar a interceptação, mas não a regra específica. "
            "O fornecedor do portal pode consultar o cf-ray nos registros do Cloudflare."
        )
    elif status is not None and status >= 400:
        classification = f"resposta HTTP {status} antes da grade"
        cause_limit = "O status, isoladamente, não identifica a regra ou causa no servidor."
    else:
        classification = "conteúdo esperado do portal não foi localizado"
        cause_limit = "Verifique a resposta e o conteúdo abaixo; a causa não foi identificada como Cloudflare."

    diagnostic = {
        "registrado_em": datetime.now(timezone.utc).isoformat(),
        "url_solicitada": CONTRACTS_URL,
        "url_resposta": response_url,
        "status_http": status,
        "classificacao": classification,
        "sinais_cloudflare": cloudflare_signals,
        "cabecalhos_relevantes": headers,
        "titulo": title,
        "trecho_corpo": body,
        "urls_frames": frame_urls,
        "erro_playwright": error,
        "limite_diagnostico": cause_limit,
    }
    DIAGNOSTICS_DIR.mkdir(parents=True, exist_ok=True)
    output = DIAGNOSTICS_DIR / "portal_acesso.json"
    output.write_text(json.dumps(diagnostic, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")
    print("Diagnóstico de acesso ao portal:", flush=True)
    print(json.dumps(diagnostic, ensure_ascii=False, indent=2), flush=True)
    print(f"Diagnóstico gravado em: {output}", flush=True)
    return output


async def open_contract_list(page: Page) -> Frame:
    # Rota interna pública do próprio link Contratos → Contratos. Abre a grade
    # diretamente e evita o menu de cards, cuja animação intercepta cliques.
    response = None
    try:
        response = await page.goto(CONTRACTS_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(1400)
        frame = await web_frame(page, require_contracts=True)
        page_text = (await frame.locator("body").inner_text()).upper()
        if "CONTRATOS" not in page_text or "REGISTRO" not in page_text:
            raise RuntimeError("A página carregou, mas não contém a grade esperada de contratos.")
        return frame
    except Exception as exc:
        diagnostic_path = await save_portal_access_diagnostic(page, response, str(exc))
        raise RuntimeError(
            f"Falha ao abrir a listagem do portal. Diagnóstico salvo em {diagnostic_path}. {exc}"
        ) from exc


async def visible_inputs(frame: Frame):
    return frame.locator("input:visible")


async def set_period(page: Page, ini: date, fim: date):
    """Reencontra a tela e seus controles a cada janela, pois o portal recria o frame."""
    frame = await web_frame(page, require_contracts=True)
    # Primeiro tenta os IDs observados no formulário do portal.
    date_inputs = []
    for selector in ("#WFRInput1082838", "#WFRInput1082837"):
        loc = frame.locator(selector)
        if await loc.count():
            date_inputs.append(loc.first)
    if len(date_inputs) < 2:
        inputs = await frame.locator("input:visible").all()
        for loc in inputs:
            try:
                val = (await loc.input_value(timeout=500)).strip()
                typ = await loc.get_attribute("type")
                if typ == "date" or re.fullmatch(r"\d{2}/\d{2}/\d{4}", val):
                    date_inputs.append(loc)
            except Exception:
                continue
    if len(date_inputs) < 2:
        raise RuntimeError("Não encontrei dois campos de período na tela de contratos; frame atual: " + frame.url)
    # O SmartClient pode re-renderizar a tela ao alterar cada campo. Além
    # disso, os campos podem validar o intervalo entre si: se o novo início
    # estiver depois do fim atualmente preenchido, atualizamos o fim primeiro.
    requested_start, requested_end = br_date(ini), br_date(fim)
    start_selector, end_selector = "#WFRInput1082838", "#WFRInput1082837"
    start_loc, end_loc = frame.locator(start_selector).first, frame.locator(end_selector).first
    current_start = (await start_loc.input_value()).strip()
    current_end = (await end_loc.input_value()).strip()

    def parsed_br(value: str) -> date | None:
        try:
            return datetime.strptime(value, "%d/%m/%Y").date()
        except (TypeError, ValueError):
            return None

    old_start, old_end = parsed_br(current_start), parsed_br(current_end)
    new_start, new_end = ini, fim
    if old_end is not None and new_start >= old_end:
        changes = ((end_selector, requested_end), (start_selector, requested_start))
    elif old_start is not None and new_end < old_start:
        changes = ((start_selector, requested_start), (end_selector, requested_end))
    else:
        changes = ((start_selector, requested_start), (end_selector, requested_end))

    for selector, requested in changes:
        # Reencontra o elemento a cada operação; o SmartClient pode substituir
        # o input anterior depois do evento disparado pelo fill().
        loc = frame.locator(selector).first
        await loc.fill(requested)
        await page.wait_for_timeout(300)
        actual = (await frame.locator(selector).first.input_value()).strip()
        if actual != requested:
            attrs = await frame.locator(selector).first.evaluate("el => ({id:el.id, type:el.type, min:el.min, max:el.max, value:el.value, title:el.title, placeholder:el.placeholder})")
            raise RuntimeError(
                f"O campo de período {attrs.get('id')} não manteve {requested}; "
                f"valor atual: {actual!r}; atributos: {attrs}. "
                f"Intervalo antes do preenchimento: {current_start} a {current_end}."
            )
        await page.wait_for_timeout(250)
    # O SmartClient expõe o tooltip no atributo data-bs-title, então o nome
    # acessível do botão pode não corresponder ao texto visível.
    apply_filter = frame.locator(
        'button[data-bs-title="Aplica Filtro Selecionado"], '
        'button[data-bs-original-title="Aplica Filtro Selecionado"]'
    )
    if await apply_filter.count() == 0:
        apply_filter = frame.locator("button").filter(
            has_text=re.compile(r"^\s*Aplicar Filtro\s*$", re.I)
        )
    if await apply_filter.count() == 0:
        apply_filter = frame.get_by_text("Aplicar Filtro", exact=True)
    if await apply_filter.count() == 0:
        raise RuntimeError("Não localizei o botão de aplicar o período; texto do frame: " +
                           (await frame.locator("body").inner_text())[:1200])
    # Em headless, o SmartClient às vezes mantém o botão correto no DOM, mas
    # sem caixa visível (0x0). Tenta primeiro a interação normal; nesse caso
    # específico usa HTMLElement.click() no próprio botão e valida que a grade
    # refletiu o intervalo pedido.
    old_rows = await frame.locator("table.listTable tbody tr").count()
    last_values = ("", "")
    for attempt in range(2):
        try:
            await apply_filter.first.click(timeout=2500)
        except PlaywrightTimeout:
            await apply_filter.first.evaluate("el => el.click()")
        await page.wait_for_timeout(1400 + attempt * 1000)
        frame = await web_frame(page, require_contracts=True)
        new_start = frame.locator("#WFRInput1082838")
        new_end = frame.locator("#WFRInput1082837")
        if await new_start.count() and await new_end.count():
            last_values = ((await new_start.first.input_value()).strip(),
                           (await new_end.first.input_value()).strip())
            if last_values == (requested_start, requested_end):
                return frame
        # A troca do frame invalida locators anteriores. Relocaliza o botão e
        # restaura as datas para uma tentativa adicional.
        if attempt == 0:
            for selector, requested in zip(("#WFRInput1082838", "#WFRInput1082837"),
                                           (requested_start, requested_end)):
                field = frame.locator(selector).first
                await field.fill(requested)
                await page.wait_for_timeout(250)
            apply_filter = frame.locator(
                'button[data-bs-title="Aplica Filtro Selecionado"], '
                'button[data-bs-original-title="Aplica Filtro Selecionado"]'
            )
            if await apply_filter.count() == 0:
                apply_filter = frame.locator("button").filter(
                    has_text=re.compile(r"^\s*Aplicar Filtro\s*$", re.I)
                )
    raise RuntimeError(
        "O portal não aplicou o período solicitado após duas tentativas: "
        f"solicitado {requested_start} a {requested_end}; exibido "
        f"{last_values[0]} a {last_values[1]}. Grade tinha {old_rows} linhas."
    )


async def _visible_contract_rows(frame: Frame) -> list[dict[str, Any]]:
    """Lê as linhas de contrato atualmente renderizadas pela grade."""
    rows = await frame.locator("table.listTable tbody tr").evaluate_all("""trs => trs.map(tr =>
      Array.from(tr.querySelectorAll('td')).map(td => (td.innerText || '').trim()))""")
    found, seen = [], {}
    for cells in rows:
        # A grade row is identified by its numeric display index, not by the
        # contract number format. The portal also uses 001-2021, 000010/2021,
        # and occasionally leaves the contract number cell blank.
        if len(cells) < 18 or not re.fullmatch(r"\d+", cells[0]):
            continue
        numero_portal = cells[1].strip()
        # O número exibido não é necessariamente único no portal. A chave usa
        # também a linha completa para não descartar contratos distintos que
        # compartilhem o mesmo número; linhas idênticas continuam deduplicadas.
        digest = hashlib.sha1("|".join(cells).encode("utf-8")).hexdigest()[:12]
        if numero_portal:
            key = f"{numero_portal}#{digest}"
            numero = numero_portal
        else:
            key = f"sem-numero-{digest}"
            numero = f"SEM-NUMERO-{digest}"
        if key in seen:
            if seen[key] == cells:
                continue  # mesma linha repetida nas tabelas internas do SmartClient
            digest = hashlib.sha1("|".join(cells).encode("utf-8")).hexdigest()[:12]
            key = f"{key}-{digest}"
            if not numero_portal:
                numero = f"SEM-NUMERO-{digest}"
        seen[key] = cells
        found.append({"numero": numero, "numero_portal": numero_portal,
          "chave_registro": key, "licitacao": cells[2], "data_assinatura": cells[3],
          "inicio_vigencia": cells[4], "fim_vigencia": cells[5], "orgao": cells[6],
          "tipo_contrato": cells[7], "contratado": cells[8], "documento_contratado": cells[9],
          "objeto": cells[10], "valor": cells[11], "aditivos": cells[12],
          "situacao": cells[17], "emergencial": cells[18] if len(cells) > 18 else None,
          "linha_grade": cells})
    return found


async def _grid_scroller(frame: Frame):
    """Localiza a área rolável interna da grade SmartClient."""
    candidates = frame.locator("#containergrade .listGrid div")
    states = await candidates.evaluate_all("""els => els.map((e, index) => ({
      index, id:e.id, hasTable:!!e.querySelector('table.listTable'),
      top:e.scrollTop, height:e.scrollHeight, client:e.clientHeight,
      overflow:getComputedStyle(e).overflowY
    }))""")
    matches = [s for s in states if s["hasTable"] and s["height"] > s["client"] + 20
               and s["client"] > 100 and s["overflow"] in ("hidden", "auto", "scroll")]
    if not matches:
        matches = [s for s in states if s["hasTable"] and s["height"] > s["client"] + 20
                   and s["client"] > 100]
    return candidates.nth(matches[0]["index"]) if matches else None


async def _set_grid_scroll(scroller, value: int):
    return await scroller.evaluate("""(el, top) => {
      el.scrollTop = top;
      el.dispatchEvent(new Event('scroll', {bubbles:true}));
      return {top:el.scrollTop, max:Math.max(0, el.scrollHeight-el.clientHeight),
              client:el.clientHeight};
    }""", value)


async def _validate_grid_count(frame: Frame, rows: list[dict[str, Any]]):
    summary = await frame.locator("#containergrade").inner_text()
    match = re.search(r"(\d+)\s+Registro\(s\)", summary, flags=re.I)
    if match and int(match.group(1)) > len(rows):
        raise RuntimeError(
            f"A grade informa {match.group(1)} registros, mas só consegui ler {len(rows)}. "
            "A extração foi interrompida para evitar salvar uma lista incompleta."
        )


async def _expected_grid_count(frame: Frame) -> int | None:
    summary = await frame.locator("#containergrade").inner_text()
    match = re.search(r"(\d+)\s+Registro\(s\)", summary, flags=re.I)
    return int(match.group(1)) if match else None


async def contract_rows(frame: Frame, page: Page | None = None) -> list[dict[str, Any]]:
    """Percorre a rolagem virtualizada e reúne todas as linhas da grade."""
    scroller = await _grid_scroller(frame)
    if scroller is None:
        result = await _visible_contract_rows(frame)
        await _validate_grid_count(frame, result)
        return result
    collected: dict[str, dict[str, Any]] = {}
    expected_count = await _expected_grid_count(frame)
    last_count = 0
    # Uma nova varredura completa recupera linhas que o SmartClient não tenha
    # renderizado a tempo em algum ponto do scroll virtualizado.
    for scan in range(1, 4):
        await _set_grid_scroll(scroller, 0)
        if page:
            await page.wait_for_timeout(350)
        for step in range(20000):
            for row in await _visible_contract_rows(frame):
                collected.setdefault(row["chave_registro"], row)
            current = await scroller.evaluate("el => ({top:el.scrollTop,max:Math.max(0,el.scrollHeight-el.clientHeight),client:el.clientHeight})")
            if current["top"] >= current["max"] - 2:
                # Ler mais uma vez no fim: algumas linhas aparecem depois do
                # evento de scroll, sem outro movimento da barra.
                if page:
                    await page.wait_for_timeout(350)
                for row in await _visible_contract_rows(frame):
                    collected.setdefault(row["chave_registro"], row)
                break
            # Passos curtos com sobreposição generosa; evita saltar linhas
            # enquanto a grade troca seu conjunto virtualizado.
            stride = max(100, int(current["client"] * 0.18))
            target = min(current["top"] + stride, current["max"])
            moved = await _set_grid_scroll(scroller, target)
            if moved["top"] <= current["top"]:
                if page:
                    await scroller.hover()
                    await page.mouse.wheel(0, stride)
                else:
                    break
            if page:
                await page.wait_for_timeout(350)
            if page and step and step % 50 == 0:
                print(f"  Varredura {scan}/3: {len(collected)} contrato(s) identificados...", flush=True)
        last_count = len(collected)
        if expected_count is None or last_count >= expected_count:
            break
        print(f"  Varredura {scan}/3 leu {last_count} de {expected_count}; repetindo a grade...", flush=True)
    # Deixa a grade no início para que a primeira lupa esteja acessível.
    await _set_grid_scroll(scroller, 0)
    if page:
        await page.wait_for_timeout(150)
    result = list(collected.values())
    # A barra de resumo do SmartClient informa o total do filtro. Não prossiga
    # silenciosamente com uma extração parcial se a rolagem não alcançou tudo.
    if expected_count is not None and len(result) < expected_count:
        found_indices = {int(row["linha_grade"][0]) for row in result
                         if row.get("linha_grade") and str(row["linha_grade"][0]).isdigit()}
        missing = [str(i) for i in range(1, expected_count + 1) if i not in found_indices]
        preview = ", ".join(missing[:20]) or "não identificáveis"
        raise RuntimeError(
            f"A grade informa {expected_count} registros, mas só consegui ler {len(result)} "
            f"após 3 varreduras. Índices ausentes (amostra): {preview}. "
            "A extração foi interrompida para evitar salvar uma lista incompleta."
        )
    await _validate_grid_count(frame, result)
    return result


async def locate_contract_row(frame: Frame, row: dict[str, Any]):
    """Localiza linha pelo número público ou pelo conteúdo integral da linha."""
    expected = row.get("linha_grade") or []
    if expected:
        trs = frame.locator("table.listTable tbody tr")
        for i in range(await trs.count()):
            tr = trs.nth(i)
            actual = [" ".join(value.split()) for value in await tr.locator("td").all_text_contents()]
            expected_normalized = [" ".join(str(value).split()) for value in expected]
            if actual == expected_normalized:
                return tr
    numero_portal = (row.get("numero_portal") or row.get("numero") or "").strip()
    if numero_portal and not numero_portal.startswith("SEM-NUMERO-"):
        locator = frame.get_by_text(numero_portal, exact=True)
        if await locator.count():
            return locator.first.locator("xpath=ancestor::tr[1]")
    return None


async def ensure_contract_visible(frame: Frame, page: Page, row: dict[str, Any]):
    """Rola a grade até a linha pedida se ela ainda não estiver no DOM visível."""
    target = await locate_contract_row(frame, row)
    if target is not None:
        return
    scroller = await _grid_scroller(frame)
    if scroller is None:
        raise RuntimeError("Não localizei a área rolável da grade para reencontrar o contrato.")
    expected = row.get("linha_grade") or []
    target_index = int(expected[0]) if expected and str(expected[0]).isdigit() else None

    # O SmartClient recicla as linhas do DOM. Se o índice desejado já está
    # dentro da faixa que foi percorrida, ele pode ter sido reciclado e não
    # aparecer mais no DOM atual. Nesse caso, reinicie do topo em vez de seguir
    # rolando para baixo e perder a linha definitivamente.
    visible_indices = await frame.locator("table.listTable tbody tr").evaluate_all(
        "trs => trs.map(tr => (tr.querySelector('td')?.innerText || '').trim())"
    )
    visible_indices = [int(value) for value in visible_indices if value.isdigit()]
    if target_index is not None and visible_indices and target_index <= max(visible_indices):
        await _set_grid_scroll(scroller, 0)
        await page.wait_for_timeout(300)
        frame = await web_frame(page, require_contracts=True)
        target = await locate_contract_row(frame, row)
        if target is not None:
            return

    for _ in range(20000):
        current = await scroller.evaluate("el => ({top:el.scrollTop,max:Math.max(0,el.scrollHeight-el.clientHeight),client:el.clientHeight})")
        if current["top"] >= current["max"] - 2:
            break
        stride = max(80, int(current["client"] * 0.15))
        moved = await _set_grid_scroll(scroller, min(current["top"] + stride, current["max"]))
        if moved["top"] <= current["top"]:
            await scroller.hover()
            await page.mouse.wheel(0, stride)
        await page.wait_for_timeout(300)
        frame = await web_frame(page, require_contracts=True)
        target = await locate_contract_row(frame, row)
        if target is not None:
            return
    raise RuntimeError(
        f"Não reencontrei o contrato {row.get('numero')} na grade após percorrê-la "
        f"(índice desejado: {target_index if target_index is not None else 'indisponível'}; "
        f"faixa visível ao iniciar a busca: "
        f"{min(visible_indices) if visible_indices else '?'}–{max(visible_indices) if visible_indices else '?'})."
    )


async def wait_for_manual_filter(page: Page, ini: date, fim: date):
    """Aguarda o operador aplicar o período e autorizar a leitura da grade."""
    expected_start, expected_end = br_date(ini), br_date(fim)
    while True:
        await asyncio.to_thread(input,
            f"\nNo Chrome, informe {expected_start} a {expected_end}, aplique o filtro "
            "e confira a grade. Pressione Enter aqui quando estiver pronta... ")
        frame = await web_frame(page, require_contracts=True)
        start_field = frame.locator("#WFRInput1082838")
        end_field = frame.locator("#WFRInput1082837")
        if not await start_field.count() or not await end_field.count():
            print("Não encontrei os campos de período na tela. Confira se a grade de Contratos está aberta.", flush=True)
            continue
        shown_start = (await start_field.first.input_value()).strip()
        shown_end = (await end_field.first.input_value()).strip()
        rows = await contract_rows(frame, page)
        eligible, ignored = exclude_ignored_orgao(rows)
        print(f"Período nos campos: {shown_start} a {shown_end}; registros lidos na grade: {len(rows)}", flush=True)
        if ignored:
            print(f"  {ignored} contrato(s) da CÂMARA MUNICIPAL DE TEIXEIRA DE FREITAS serão ignorados.", flush=True)
        print(f"  Fila de extração: {len(eligible)} contrato(s).", flush=True)
        print("Contratos na fila: " + (", ".join(r["numero"] for r in eligible[:8]) or "nenhum"), flush=True)
        if (shown_start, shown_end) != (expected_start, expected_end):
            print("As datas não correspondem à janela atual. Ajuste o filtro no Chrome e tente novamente.", flush=True)
            continue
        if not rows:
            print("A grade está sem contratos. Ajuste ou reaplique o filtro antes de autorizar.", flush=True)
            continue
        if not eligible:
            print("Todos os registros desta janela são da Câmara e serão ignorados.", flush=True)
            return frame, rows
        answer = await asyncio.to_thread(input,
            f"Digite EXTRAIR para autorizar a extração destes {len(eligible)} contratos, "
            "ou pressione Enter para revisar o filtro: ")
        if answer.strip().upper() == "EXTRAIR":
            return frame, rows


async def _extract_detail_and_items_once(context, list_page: Page, row: dict[str, Any]) -> dict[str, Any]:
    # A lupa na coluna de ações abre os detalhes em uma nova guia.
    # O documento externo também contém textos sobre contratos; exigir o frame
    # da grade evita procurar o número no frame de navegação errado.
    frame = await web_frame(list_page, require_contracts=True)
    await ensure_contract_visible(frame, list_page, row)
    frame = await web_frame(list_page, require_contracts=True)
    tr = await locate_contract_row(frame, row)
    if tr is None:
        return {**row, "erro_detalhe": "linha não localizável para abrir detalhes"}
    try:
        detail_icon = tr.locator("td").nth(13).locator("img")
        detail = None
        last_error = None
        for attempt in range(1, 4):
            pages_before = set(context.pages)
            try:
                await detail_icon.scroll_into_view_if_needed(timeout=5000)
                async with context.expect_page(timeout=20000) as detail_info:
                    await detail_icon.click(timeout=8000, force=(attempt > 1))
                detail = await detail_info.value
                break
            except PlaywrightTimeout as e:
                last_error = e
                # Em alguns carregamentos o evento de popup chega depois do timeout.
                opened = [p for p in context.pages if p not in pages_before]
                if opened:
                    detail = opened[-1]
                    break
                if attempt < 3:
                    await list_page.wait_for_timeout(700 * attempt)
        if detail is None:
            return {**row, "erro_detalhe": f"lupa não abriu guia após 3 tentativas: {last_error}"}
    except Exception as e:
        return {**row, "erro_detalhe": f"falha ao acionar lupa: {type(e).__name__}: {e}"}
    await detail.wait_for_load_state("domcontentloaded")
    try:
        detail_frame = await web_frame(detail)
    except Exception as e:
        await detail.close()
        return {**row, "erro_detalhe": str(e), "url_detalhe": detail.url}
    detail_text = await detail_frame.locator("body").inner_text()
    result: dict[str, Any] = {**row, "detalhes_texto": detail_text}
    result["detalhes_campos"] = await detail_frame.locator("input").evaluate_all(
        "els => els.map(e => ({id:e.id, nome:e.name, valor:e.value, tipo:e.type}))")
    numero_portal = (row.get("numero_portal") or "").strip()
    if numero_portal:
        field_values = [str(field.get("valor", "")).strip() for field in result["detalhes_campos"]]
        if numero_portal not in detail_text and numero_portal not in field_values:
            await detail.close()
            return {**result, "erro_validacao_contrato":
                    f"Os detalhes abertos não confirmam o contrato {numero_portal}."}
    m = re.search(r"CHAVE=([^@&\s]+)", detail.url, flags=re.I)
    key = m.group(1) if m else None
    if key:
        result["chave_portal"] = key
    items_page = None
    try:
        # O botão é anunciado pelo tooltip acessível como “Itens do Contrato”.
        item_control = detail_frame.locator("#ItemContrato")
        async with context.expect_page(timeout=8000) as item_info:
            await item_control.click()
        items_page = await item_info.value
        await items_page.wait_for_load_state("domcontentloaded")
        items_frame = await web_frame(items_page)
        if await items_frame.locator("table.listTable").count() == 0:
            raise RuntimeError("A guia abriu, mas a grade de itens não foi localizada.")
        result["itens"] = await items_frame.locator("table.listTable tbody tr").evaluate_all("""trs => trs.map(tr =>
          Array.from(tr.querySelectorAll('td')).map(td => (td.innerText || '').trim()))
          .filter(c => c.length >= 7 && /^\\d+$/.test(c[0]))
          .map(c => ({descricao:c[1], quantidade:c[2], periodicidade:c[3], lote:c[4], valor_unitario:c[5], valor_total:c[6]}))""")
        result["itens_texto"] = await items_frame.locator("body").inner_text()
        item_summary = items_frame.locator("#containergrade")
        summary_text = (await item_summary.inner_text()) if await item_summary.count() else result["itens_texto"]
        count_match = re.search(r"(\d+)\s+Registro\(s\)", summary_text, flags=re.I)
        explicit_empty = bool(count_match and int(count_match.group(1)) == 0) or bool(
            re.search(r"não existem dados|nenhum item|nenhum registro", result["itens_texto"], re.I)
        )
        if count_match and int(count_match.group(1)) > len(result["itens"]):
            raise RuntimeError(
                f"A tela de itens informa {count_match.group(1)} registro(s), "
                f"mas o parser leu {len(result['itens'])}."
            )
        if not result["itens"] and not explicit_empty:
            raise RuntimeError("A guia de itens abriu, mas não confirmou itens nem uma lista vazia.")
        result["itens_extraidos"] = True
        result["itens_quantidade"] = len(result["itens"])
    except Exception as e:
        result["erro_itens"] = str(e)
    finally:
        if items_page and not items_page.is_closed():
            await items_page.close()
    await detail.close()
    return result


async def extract_detail_and_items(context, list_page: Page, row: dict[str, Any]) -> dict[str, Any]:
    """Repete a navegação a partir da lista até confirmar a extração dos itens."""
    last_result: dict[str, Any] = {**row}
    for attempt in range(1, 4):
        try:
            result = await _extract_detail_and_items_once(context, list_page, row)
        except Exception as exc:
            result = {**row, "erro_extracao": f"{type(exc).__name__}: {exc}"}
        if result.get("itens_extraidos") is True and not any(
                result.get(k) for k in ("erro_detalhe", "erro_extracao", "erro_validacao_contrato", "erro_itens")):
            if attempt > 1:
                print(f"  Itens confirmados na tentativa {attempt} para {row['numero']}", flush=True)
            return result
        last_result = result
        failure = (result.get("erro_detalhe") or result.get("erro_extracao") or
                   result.get("erro_validacao_contrato") or result.get("erro_itens") or
                   "a extração dos itens não foi confirmada")
        print(f"  Tentativa {attempt}/3 sem confirmação de detalhes e itens para {row['numero']}: {failure}", flush=True)
        for extra_page in list(context.pages):
            if extra_page != list_page and not extra_page.is_closed():
                await extra_page.close()
        if attempt < 3:
            await list_page.wait_for_timeout(700 * attempt)
    last_result["erro_itens"] = (
        "Itens não confirmados após 3 tentativas; o registro ficará pendente para nova execução. "
        + str(last_result.get("erro_itens") or last_result.get("erro_validacao_contrato") or
              last_result.get("erro_detalhe") or last_result.get("erro_extracao") or "")
    )
    last_result.pop("itens_extraidos", None)
    return last_result


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


async def run(args):
    print(f"Scraper manual versão {SCRIPT_VERSION}", flush=True)
    today = args.today or date.today()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    data = load_json(OUT, {"fonte": HOME, "contratos": []})
    state = load_json(STATE, {"ultima_data_coberta": None})
    indexed = {(c.get("chave_registro") or c.get("numero")): c
               for c in data.get("contratos", [])
               if c.get("chave_registro") or c.get("numero")}
    last_done = date.fromisoformat(state["ultima_data_coberta"]) if state.get("ultima_data_coberta") else None
    if args.start:
        # --start explícito sempre prevalece sobre o checkpoint.
        start = args.start
    elif last_done and not args.full:
        # Sem --start, a execução é incremental: reconsulta uma cauda curta.
        start = max(date(2021, 1, 1), last_done - timedelta(days=OVERLAP_DAYS))
    else:
        start = date(2021, 1, 1)
    async with async_playwright() as p:
        if args.manual_filter and args.headless:
            raise ValueError("--manual-filter exige o Chrome visível; remova --headless.")
        browser = await p.chromium.launch(headless=args.headless and not args.manual_filter)
        context = await browser.new_context(viewport={"width": 1500, "height": 950})
        page = await context.new_page()
        frame = await open_contract_list(page)
        for ini, fim in windows(start, today, args.window_days):
            print(f"Consultando {br_date(ini)} a {br_date(fim)}", flush=True)
            if args.manual_filter:
                frame, rows = await wait_for_manual_filter(page, ini, fim)
            else:
                frame = await set_period(page, ini, fim)
                rows = await contract_rows(frame, page)
            rows, ignored_count = exclude_ignored_orgao(rows)
            if ignored_count:
                print(f"  Ignorados {ignored_count} contrato(s) da CÂMARA MUNICIPAL DE TEIXEIRA DE FREITAS.", flush=True)
            # Diagnóstico de filtro: valores atuais e quantidade de linhas na grade.
            print(f"  {len(rows)} contrato(s) na janela {br_date(ini)}–{br_date(fim)}", flush=True)
            # Fase 1 encerra aqui: a grade foi percorrida, validada contra o
            # contador do portal e o scroll retornou ao topo. Congela a fila.
            # Ordena pelo índice sequencial exibido na primeira coluna. Assim
            # a fase de extração acompanha o scroll da grade de cima para baixo.
            snapshot = tuple(sorted(
                (dict(row) for row in rows),
                key=lambda item: int(item["linha_grade"][0])
                if item.get("linha_grade") and str(item["linha_grade"][0]).isdigit()
                else 10**12,
            ))
            print(f"  Fase 1/2 concluída: {len(snapshot)} contrato(s) registrados na fila. "
                  "Iniciando detalhes e itens.", flush=True)
            if not rows:
                try:
                    current = await frame.locator("input:visible").evaluate_all("els => els.map(e => ({id:e.id,value:e.value,type:e.type}))")
                    grid_count = await frame.locator("table.listTable tbody tr").count()
                    print(f"  Diagnóstico grade: {grid_count} linha(s); campos visíveis: {current}", flush=True)
                except Exception:
                    pass
            skipped_complete = 0
            # Fase 2 opera sobre a fotografia, não sobre linhas relidas do DOM.
            for position, row in enumerate(snapshot, start=1):
                record_key = row.get("chave_registro") or row["numero"]
                existing = indexed.get(record_key) or indexed.get(row["numero"])
                had_error = bool(existing and any(existing.get(k) for k in
                    ("erro_detalhe", "erro_extracao", "erro_validacao_contrato", "erro_itens")))
                # Considere concluído somente se detalhes e a consulta da lupa
                # de itens foram salvos e a rotina marcou a leitura como válida.
                has_detail = bool(existing and (
                    existing.get("detalhes_texto") or existing.get("detalhes_campos")
                ))
                has_items_result = bool(existing and existing.get("itens_extraidos") is True
                                        and "itens" in existing and "itens_texto" in existing)
                if existing and not args.refresh and has_detail and has_items_result and not had_error:
                    skipped_complete += 1
                    continue
                try:
                    print(f"  Extraindo {position}/{len(snapshot)}: {row['numero']}", flush=True)
                    detail = await extract_detail_and_items(context, page, row)
                except Exception as e:
                    detail = {**row, "erro_extracao": f"{type(e).__name__}: {e}"}
                    print(f"  Falha no contrato {row['numero']}: {detail['erro_extracao']}", flush=True)
                detail["periodo_consulta"] = {"inicio": br_date(ini), "fim": br_date(fim)}
                indexed[record_key] = detail
                data["contratos"] = list(indexed.values())
                OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                if detail.get("itens_extraidos") is True:
                    print(f"  Processado {row['numero']} — {detail.get('itens_quantidade', 0)} item(ns) confirmado(s)", flush=True)
                else:
                    print(f"  PENDENTE {row['numero']} — itens não confirmados; será tentado novamente", flush=True)
            if skipped_complete:
                print(f"  {skipped_complete} contrato(s) já tinham detalhes e resultado da consulta de itens; ignorados", flush=True)
            state["ultima_data_coberta"] = fim.isoformat()
            state["executado_em"] = datetime.now().isoformat(timespec="seconds")
            STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        await browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=iso_date, help="data inicial (AAAA-MM-DD); sem ela, retoma pelo checkpoint")
    parser.add_argument("--today", type=iso_date, help="data final de referência (AAAA-MM-DD; para teste/reprodução)")
    parser.add_argument("--window-days", type=positive_int, default=DAY_WINDOW,
                        help="dias por janela de consulta (padrão: 10; ex.: --window-days 30)")
    parser.add_argument("--headless", action="store_true", help="roda navegador sem janela")
    parser.add_argument("--manual-filter", action="store_true",
                        help="abre o Chrome visível; você aplica cada janela e digita EXTRAIR para autorizar")
    parser.add_argument("--full", action="store_true", help="ignora checkpoint e refaz todo o histórico")
    parser.add_argument("--refresh", action="store_true", help="atualiza contratos existentes")
    asyncio.run(run(parser.parse_args()))
