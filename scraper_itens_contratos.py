#!/usr/bin/env python3
"""Complementa um JSON de contratos com os itens da lupa do portal.

O navegador fica visível. A pessoa aplica manualmente a listagem ampla e
confirma; depois o script pesquisa cada número, valida a linha e salva após
cada contrato confirmado. Registros e itens anteriores são preservados.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DEFAULT_JSON = ROOT / "dados" / "resultcontratos.json"
BASE_SCRIPT = ROOT / "scraper_contratos-manual.py"


def load_json(path: Path) -> Any:
    try:
        # Parse directly from the text stream. read_bytes() followed by
        # decode() briefly held two full copies of a potentially large file.
        with path.open("r", encoding="utf-8-sig") as source:
            return json.load(source)
    except UnicodeDecodeError:
        with path.open("r", encoding="cp1252") as source:
            return json.load(source)


def save_json(path: Path, payload: Any) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    encoder = json.JSONEncoder(ensure_ascii=False, indent=2)
    with temp.open("w", encoding="utf-8", newline="\n") as target:
        for chunk in encoder.iterencode(payload):
            target.write(chunk)
        target.write("\n")
    temp.replace(path)


def norm(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def normalize_contract_fields(records: list[Any]) -> None:
    """Add the scraper's canonical field names to raw portal rows in place."""
    aliases = {
        "numero": ("NUMERO_CONTRATO", "Número do Contrato"),
        "licitacao": ("LIC_NUMERO",),
        "data_assinatura": ("DATA_CONTRATO",),
        "inicio_vigencia": ("DATA_INICIO",),
        "fim_vigencia": ("DATA_TERMINO",),
        "orgao": ("ORG_NOME",),
        "tipo_contrato": ("TIPO_CONTRATO",),
        "contratado": ("CREDOR",),
        "documento_contratado": ("DOCUMENTO",),
        "objeto": ("CTR_OBJETIVO",),
        "valor": ("VALOR_CONTRATO",),
        "aditivos": ("QT.Adt.",),
        "situacao": ("Situação",),
        "emergencial": ("DESASTRE",),
        "chave_portal": ("CHAVE",),
    }
    for record in records:
        if not isinstance(record, dict):
            continue
        for canonical, source_names in aliases.items():
            if record.get(canonical) not in (None, ""):
                continue
            for source_name in source_names:
                if record.get(source_name) not in (None, ""):
                    record[canonical] = record[source_name]
                    break


def import_base():
    spec = importlib.util.spec_from_file_location("scraper_contratos_manual", BASE_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Não foi possível carregar {BASE_SCRIPT.name}.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def contract_matches(row: dict[str, Any], contract: dict[str, Any]) -> bool:
    number = norm(contract.get("numero"))
    if not number or number.startswith("sem-numero") or norm(row.get("numero")) != number:
        return False
    # O portal pode repetir o número. Use organização e fornecedor para
    # distinguir registros homônimos, quando esses dados existirem no JSON.
    for field in ("orgao", "contratado"):
        wanted, actual = norm(contract.get(field)), norm(row.get(field))
        if wanted and actual and wanted != actual:
            return False
    return True


def confirm_items(detail: dict[str, Any]) -> tuple[bool, str]:
    """Validate extracted rows even if an older helper omits its success flag."""
    items = detail.get("itens")
    items_text = str(detail.get("itens_texto") or "")
    if not isinstance(items, list):
        return False, "A guia de itens não retornou uma lista estruturada."

    count_match = re.search(r"(\d+)\s+Registro\(s\)", items_text, flags=re.I)
    if count_match:
        expected = int(count_match.group(1))
        if expected != len(items):
            return False, f"Portal informa {expected} registro(s), mas foram lidos {len(items)} item(ns)."
        detail["itens_extraidos"] = True
        detail["itens_quantidade"] = len(items)
        return True, ""

    if items:
        # Compatibilidade com scraper auxiliar anterior que retorna as linhas
        # e o texto da grade, mas ainda não inclui itens_extraidos/count.
        if not items_text.strip():
            return False, "Itens foram retornados, mas o texto de confirmação da tela está vazio."
        detail["itens_extraidos"] = True
        detail["itens_quantidade"] = len(items)
        return True, ""

    if re.search(r"não existem dados|nenhum item|nenhum registro|0\s+Registro\(s\)", items_text, re.I):
        detail["itens_extraidos"] = True
        detail["itens_quantidade"] = 0
        return True, ""
    return False, "A tela não confirmou itens nem confirmou que a lista está vazia."


async def grid_total(frame) -> int | None:
    """Read the portal's total count without depending on helper-script version."""
    summary = frame.locator("#containergrade")
    if not await summary.count():
        return None
    text = await summary.inner_text()
    match = re.search(r"(\d+)\s+Registro\(s\)", text, flags=re.I)
    return int(match.group(1)) if match else None


async def number_search_field(frame):
    # Identifica o campo pela posição visual sob o cabeçalho, em vez de usar
    # IDs isc_* que mudam quando o SmartClient recria a grade.
    selectors = (
        'input[placeholder="Pesquise aqui..."]',
        'input[placeholder*="Pesquise aqui"]',
    )
    search = None
    for selector in selectors:
        candidates = frame.locator(selector)
        for index in range(await candidates.count()):
            candidate = candidates.nth(index)
            if await candidate.is_visible():
                search = candidate
                break
        if search is not None:
            break
    if search is None:
        raise RuntimeError(
            "Não encontrei os campos 'Pesquise aqui...' do grid. "
            "A busca foi interrompida para não preencher Órgão ou outra coluna por engano."
        )
    header = frame.get_by_text("Nº do Contrato", exact=True).first
    header_box = await header.bounding_box() if await header.count() else None
    if header_box:
        header_center = header_box["x"] + header_box["width"] / 2
        candidates = frame.locator('input[placeholder*="Pesquise aqui"]')
        positioned = []
        for index in range(await candidates.count()):
            candidate = candidates.nth(index)
            if not await candidate.is_visible():
                continue
            box = await candidate.bounding_box()
            if box:
                center = box["x"] + box["width"] / 2
                positioned.append((abs(center - header_center), candidate))
        if positioned:
            return min(positioned, key=lambda pair: pair[0])[1]
    # Fallback conservador: primeiro campo visível, como na interface
    # observada; não recorre a IDs de controles internos.
    return search


async def apply_search(frame, page, number: str) -> None:
    search = await number_search_field(frame)
    field_id = await search.get_attribute("id") or "sem id"
    field_name = await search.get_attribute("name") or "sem name"
    print(f"  Filtro selecionado: primeiro campo 'Pesquise aqui...' (Nº do Contrato; {field_id}, {field_name})", flush=True)
    await search.click()
    await search.press("Control+A")
    await search.press_sequentially(number, delay=35)
    # Enter no próprio campo aplica o filtro da coluna. O botão azul de
    # aplicação pertence aos filtros gerais (período/órgão) e pode deixar a
    # grade sem linhas quando usado para a pesquisa de coluna.
    await search.press("Enter")
    await page.wait_for_timeout(350)


async def wait_for_number_row(base, page, number: str):
    """Allow the portal's asynchronous grid filter to finish rendering."""
    latest = []
    for _ in range(24):  # até 6 segundos
        frame = await base.web_frame(page, require_contracts=True)
        latest = await base._visible_contract_rows(frame)
        if any(norm(row.get("numero")) == norm(number) for row in latest):
            return latest
        await page.wait_for_timeout(250)
    return latest


async def clear_number_search(base, page, baseline_count: int | None = None) -> None:
    """Remove o filtro da coluna do número e restaura a grade antes da próxima busca."""
    frame = await base.web_frame(page, require_contracts=True)
    search = await number_search_field(frame)
    value = (await search.input_value()).strip()
    if value:
        await search.click()
        await search.press("Control+A")
        await search.press("Backspace")
        await search.press("Enter")
        # Espere a grade virtualizada repovoar o DOM. Não anunciar restauração
        # só porque o campo ficou vazio: o servidor ainda pode estar buscando.
        last_count = 0
        for _ in range(60):
            await page.wait_for_timeout(250)
            frame = await base.web_frame(page, require_contracts=True)
            rows = await base._visible_contract_rows(frame)
            last_count = len(rows)
            current_total = await grid_total(frame)
            restored = (current_total == baseline_count) if baseline_count is not None else bool(last_count)
            if restored and last_count:
                print(
                    f"  Grade restaurada: {current_total if current_total is not None else last_count} registro(s) "
                    f"no filtro amplo; {last_count} linha(s) visível(is).",
                    flush=True,
                )
                return
        summary = frame.locator("#containergrade")
        summary_text = (await summary.inner_text())[:300] if await summary.count() else "sem resumo do grid"
        raise RuntimeError(
            f"Campo de busca limpo, mas a grade não voltou a exibir linhas em 15 segundos "
            f"(linhas visíveis: {last_count}; resumo: {summary_text!r})."
        )


async def run(args) -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright não está instalado neste Python. Execute: "
            "pip install playwright && playwright install chromium"
        ) from exc
    base = import_base()
    payload = load_json(args.json)
    if isinstance(payload, dict) and isinstance(payload.get("contratos"), list):
        contracts = payload["contratos"]
    elif isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        contracts = payload["data"].get("result")
    else:
        contracts = payload if isinstance(payload, list) else None
    if not isinstance(contracts, list):
        raise RuntimeError("Formato JSON não reconhecido: esperava 'contratos' ou 'data.result'.")

    # Se receber o formato bruto do portal, normaliza para um envelope que
    # preserve a carga e permite gravar os campos adicionais no mesmo local.
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        payload = {"fonte": base.HOME, "contratos": contracts}
        contracts = payload["contratos"]
    normalize_contract_fields(contracts)

    eligible = []
    missing_number = 0
    completed = 0
    for item in contracts:
        if not isinstance(item, dict):
            continue
        if args.number and norm(item.get("numero")) != norm(args.number):
            continue
        if not args.include_completed and item.get("itens_extraidos") is True and isinstance(item.get("itens"), list):
            completed += 1
            continue
        if not item.get("numero"):
            missing_number += 1
            continue
        eligible.append(item)
    if args.limit:
        eligible = eligible[:args.limit]
    print(f"{len(eligible)} contrato(s) na fila para completar itens.", flush=True)
    if not eligible:
        print(f"Diagnóstico: {len(contracts)} registro(s) no JSON; {completed} já marcados com itens confirmados; "
              f"{missing_number} sem número reconhecido.", flush=True)
    if not eligible:
        return

    args.json.parent.mkdir(parents=True, exist_ok=True)
    if not args.no_backup:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = args.json.with_name(f"{args.json.stem}.backup-{stamp}{args.json.suffix}")
        shutil.copy2(args.json, backup)
        print(f"Cópia de segurança: {backup}", flush=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(viewport={"width": 1500, "height": 950})
        page = await context.new_page()
        frame = await base.open_contract_list(page)
        print("No Chrome, aplique manualmente um filtro que inclua os contratos da fila.", flush=True)
        input("Quando a listagem estiver carregada, pressione Enter aqui para iniciar a busca por número... ")
        frame = await base.web_frame(page, require_contracts=True)
        baseline_count = await grid_total(frame)
        print(
            f"Listagem inicial: {baseline_count if baseline_count is not None else 'quantidade não informada'} "
            "registro(s) no filtro manual.", flush=True
        )

        for position, contract in enumerate(eligible, 1):
            number = str(contract.get("numero", "")).strip()
            # Contratos já completos são omitidos, salvo --include-completed.
            print(f"[{position}/{len(eligible)}] Buscando {number}", flush=True)
            reset_failed = False
            try:
                frame = await base.web_frame(page, require_contracts=True)
                await apply_search(frame, page, number)
                rows = await wait_for_number_row(base, page, number)
                same_number = [row for row in rows if norm(row.get("numero")) == norm(number)]
                # Nome do órgão/fornecedor pode variar entre a exportação e a
                # grade (acentos, abreviações ou razão social atualizada). Se
                # o número trouxe uma única linha, não descarte o resultado por
                # essa diferença. Metadados são usados para desambiguar números
                # repetidos, nunca para rejeitar um número único.
                if len(same_number) == 1:
                    matches = same_number
                else:
                    matches = [row for row in same_number if contract_matches(row, contract)]
                if len(matches) != 1:
                    found_numbers = [row.get("numero") for row in rows[:12]]
                    raise RuntimeError(
                        f"Busca encontrou {len(same_number)} linha(s) com o número {number}; "
                        f"após desambiguação restaram {len(matches)}. "
                        f"Números visíveis no grid: {found_numbers}. "
                        "Registro não alterado; confira o filtro/lista manualmente."
                    )
                detail = await base.extract_detail_and_items(context, page, matches[0])
                confirmed, confirmation_error = confirm_items(detail)
                if not confirmed:
                    errors = {
                        key: detail.get(key) for key in
                        ("erro_itens", "erro_detalhe", "erro_extracao", "erro_validacao_contrato")
                        if detail.get(key)
                    }
                    status = {
                        "itens_extraidos": detail.get("itens_extraidos"),
                        "itens_lidos": len(detail.get("itens", [])) if isinstance(detail.get("itens"), list) else None,
                        "campos_retornados": sorted(detail.keys()),
                        "erros": errors,
                        "validacao": confirmation_error,
                    }
                    raise RuntimeError(json.dumps(status, ensure_ascii=False))
                # Atualiza apenas os campos de detalhes/itens; metadados
                # originais do JSON permanecem intactos.
                for field in ("detalhes_texto", "detalhes_campos", "chave_portal", "itens", "itens_texto",
                              "itens_extraidos", "itens_quantidade"):
                    if field in detail:
                        contract[field] = detail[field]
                    elif field in ("itens", "itens_texto"):
                        contract.pop(field, None)
                for field in ("erro_detalhe", "erro_extracao", "erro_validacao_contrato", "erro_itens"):
                    contract.pop(field, None)
                contract["itens_atualizados_em"] = datetime.now().astimezone().isoformat(timespec="seconds")
                save_json(args.json, payload)
                print(f"  OK — {len(contract['itens'])} item(ns), salvo(s) no JSON.", flush=True)
            except Exception as exc:
                contract["erro_itens"] = f"{type(exc).__name__}: {exc}"
                save_json(args.json, payload)
                print(f"  PENDENTE — {type(exc).__name__}: {exc}", flush=True)
            finally:
                try:
                    await clear_number_search(base, page, baseline_count)
                    print("  Filtro de Nº do Contrato limpo; pronto para a próxima busca.", flush=True)
                except Exception as reset_error:
                    reset_failed = True
                    print(f"  ATENÇÃO — não consegui limpar o filtro antes da próxima busca: {reset_error}", flush=True)
            if reset_failed:
                print("Execução interrompida para não pesquisar os próximos contratos sobre uma grade vazia.", flush=True)
                break

        await browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", type=Path, default=DEFAULT_JSON,
                        help="JSON a complementar (padrão: dados/resultcontratos.json)")
    parser.add_argument("--number", help="processa somente este número de contrato")
    parser.add_argument("--limit", type=int, help="limita a quantidade para teste")
    parser.add_argument("--include-completed", action="store_true",
                        help="reconsulta inclusive contratos já marcados como concluídos")
    parser.add_argument("--no-backup", action="store_true", help="não criar cópia antes de iniciar")
    asyncio.run(run(parser.parse_args()))
