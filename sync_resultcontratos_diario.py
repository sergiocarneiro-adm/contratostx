#!/usr/bin/env python3
"""Sincroniza a grade atual de contratos e seus itens no JSON do index.html.

Usa os registros que o portal carrega ao abrir a tela, confere a grade inteira,
ignora contratos da Câmara e não reprocessa registros com itens confirmados.
Projetado para GitHub Actions, em Chromium headless.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import re
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
BASE_SCRIPT = ROOT / "scraper_contratos-manual.py"
ITEMS_SCRIPT = ROOT / "scraper_itens_contratos.py"
DEFAULT_JSON = ROOT / "dados" / "resultcontratos.json"
IGNORED_ORGAO = "CAMARA MUNICIPAL DE TEIXEIRA DE FREITAS"


def local_now() -> datetime:
    # Usa o fuso configurado no computador. No GitHub Actions, o workflow
    # define TZ=America/Bahia; no Windows, usa o fuso local sem exigir tzdata.
    return datetime.now().astimezone()


def load_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8-sig") as source:
            return json.load(source)
    except UnicodeDecodeError:
        with path.open("r", encoding="cp1252") as source:
            return json.load(source)


def write_json_atomic(path: Path, payload: Any) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    encoder = json.JSONEncoder(ensure_ascii=False, indent=2)
    with temp.open("w", encoding="utf-8", newline="\n") as target:
        for chunk in encoder.iterencode(payload):
            target.write(chunk)
        target.write("\n")
    temp.replace(path)


def import_scraper():
    spec = importlib.util.spec_from_file_location("scraper_contratos_manual", BASE_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Não encontrei {BASE_SCRIPT.name} ao lado deste script.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def import_items_scraper():
    """Reuse the proven search, row matching, and item-confirmation routines."""
    if not ITEMS_SCRIPT.exists():
        raise RuntimeError(
            f"Não encontrei {ITEMS_SCRIPT.name}. Mantenha esse scraper ao lado "
            "do sincronizador para reutilizar a busca por Nº do Contrato."
        )
    spec = importlib.util.spec_from_file_location("scraper_itens_contratos", ITEMS_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Não foi possível carregar {ITEMS_SCRIPT.name}.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def canonicalize_portal_fields(records: list[Any]) -> None:
    """Accept both normalized JSON and the original portal export schema."""
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
        for target, sources in aliases.items():
            if record.get(target) not in (None, ""):
                continue
            for source in sources:
                if record.get(source) not in (None, ""):
                    record[target] = record[source]
                    break


def normalized(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(c for c in text if not unicodedata.combining(c)).casefold()
    return " ".join(re.findall(r"[a-z0-9]+", text))


def identity(record: dict[str, Any]) -> tuple[str, ...]:
    # Número costuma ser único; os demais campos protegem contra números
    # reutilizados/duplicados que existem no histórico do portal.
    return tuple(normalized(record.get(field)) for field in (
        "numero", "licitacao", "data_assinatura", "orgao", "contratado"
    ))


def is_ignored(record: dict[str, Any]) -> bool:
    return normalized(record.get("orgao")) == normalized(IGNORED_ORGAO)


def has_confirmed_items(record: dict[str, Any]) -> bool:
    return record.get("itens_extraidos") is True and isinstance(record.get("itens"), list)


def as_payload(source: Any, scraper) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if isinstance(source, dict) and isinstance(source.get("contratos"), list):
        payload = source
        records = payload["contratos"]
    elif isinstance(source, dict) and isinstance(source.get("data"), dict) and isinstance(source["data"].get("result"), list):
        records = source["data"]["result"]
        payload = {"fonte": scraper.HOME, "contratos": records}
    elif isinstance(source, list):
        records = source
        payload = {"fonte": scraper.HOME, "contratos": records}
    else:
        raise RuntimeError("JSON inválido: esperava 'contratos' ou 'data.result'.")
    canonicalize_portal_fields(records)
    return payload, records


def positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Informe um número inteiro.") from exc
    if number < 1:
        raise argparse.ArgumentTypeError("O período deve ser de pelo menos 1 dia.")
    return number


async def wait_for_populated_grid(page, scraper, timeout_seconds: int = 40):
    """Wait for the portal's asynchronously loaded default contract grid."""
    deadline = time.monotonic() + timeout_seconds
    last_signature = None
    stable_polls = 0
    diagnostics = {}
    frame = None
    next_report = 5
    while time.monotonic() < deadline:
        # Select the frame that actually owns the contract grid. A surrounding
        # frame may contain the headings before the data frame has rendered.
        frame = None
        for candidate in page.frames:
            try:
                if await candidate.locator("#containergrade").count():
                    frame = candidate
                    break
            except Exception:
                continue
        if frame is None:
            frame = await scraper.web_frame(page, require_contracts=True)
        try:
            summary = (await frame.locator("#containergrade").inner_text(timeout=1500)).strip()
        except Exception:
            summary = ""
        try:
            row_count = await frame.locator("table.listTable tbody tr").count()
        except Exception:
            row_count = 0
        match = re.search(r"(\d+)\s+Registro\(s\)", summary, flags=re.I)
        expected_count = int(match.group(1)) if match else None
        diagnostics = {
            "frame": frame.url,
            "summary": summary[:1000],
            "expected_count": expected_count,
            "rendered_rows": row_count,
        }
        signature = (expected_count, row_count, summary)
        if (expected_count is not None and expected_count > 0) or row_count > 0:
            if signature == last_signature:
                stable_polls += 1
            else:
                stable_polls = 0
            if stable_polls >= 2:
                return frame
        last_signature = signature
        elapsed = int(timeout_seconds - max(0, deadline - time.monotonic()))
        if elapsed >= next_report:
            print(
                f"  Aguardando a grade ({elapsed}/{timeout_seconds}s): "
                f"{expected_count if expected_count is not None else '?'} registro(s) indicados, "
                f"{row_count} linha(s) renderizada(s).",
                flush=True,
            )
            next_report += 5
        await page.wait_for_timeout(1000)

    try:
        diagnostics["body"] = (await frame.locator("body").inner_text(timeout=1500))[:1800]
    except Exception:
        diagnostics["body"] = "Não foi possível ler o conteúdo do frame."
    raise RuntimeError(
        "A grade de Contratos não apresentou registros após aguardar "
        f"{timeout_seconds}s. Diagnóstico: {diagnostics}"
    )


async def run(args) -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError("Instale Playwright com: pip install playwright") from exc

    scraper = import_scraper()
    item_scraper = import_items_scraper()
    if args.json.exists():
        original = load_json(args.json)
    else:
        original = {"fonte": scraper.HOME, "contratos": []}
    payload, records = as_payload(original, scraper)
    lookup = {}
    for index, record in enumerate(records):
        if isinstance(record, dict) and record.get("numero"):
            lookup.setdefault(identity(record), index)

    print("Abrindo a listagem atual do portal; nenhum filtro de data será aplicado.", flush=True)
    added = updated = skipped = ignored = pending = 0

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not args.headed)
        context = await browser.new_context(viewport={"width": 1500, "height": 950})
        page = await context.new_page()
        try:
            await scraper.open_contract_list(page)
            # A tela de contratos já abre na listagem recente. Mantemos o filtro
            # padrão do portal para conferir a grade e percorrê-la por inteiro.
            print("Aguardando o carregamento da grade de contratos...", flush=True)
            frame = await wait_for_populated_grid(page, scraper)
            rows = await scraper.contract_rows(frame, page)
            if not rows:
                summary = (await frame.locator("#containergrade").inner_text())[:1000]
                raise RuntimeError(
                    "A leitura da grade terminou sem nenhum contrato, então o JSON não foi alterado. "
                    f"Resumo atual do portal: {summary!r}"
                )
            print(f"Grade conferida: {len(rows)} contrato(s) carregado(s) pelo portal.", flush=True)
            baseline_count = await item_scraper.grid_total(frame)

            new_in_window = [row for row in rows if row.get("numero") and not is_ignored(row)]
            ignored = len(rows) - len(new_in_window)
            for position, row in enumerate(new_in_window, 1):
                key = identity(row)
                existing_index = lookup.get(key)
                existing = records[existing_index] if existing_index is not None else None
                if existing is not None and has_confirmed_items(existing):
                    skipped += 1
                    continue

                label = row.get("numero") or f"linha {position}"
                print(f"[{position}/{len(new_in_window)}] Extraindo {label}", flush=True)
                try:
                    # Reproduz o fluxo comprovado pelo scraper_itens_contratos:
                    # filtra pelo Nº do Contrato antes de localizar a lupa.
                    frame = await scraper.web_frame(page, require_contracts=True)
                    await item_scraper.apply_search(frame, page, str(label))
                    filtered_rows = await item_scraper.wait_for_number_row(scraper, page, str(label))
                    same_number = [
                        candidate for candidate in filtered_rows
                        if item_scraper.norm(candidate.get("numero")) == item_scraper.norm(label)
                    ]
                    if len(same_number) == 1:
                        matches = same_number
                    else:
                        matches = [
                            candidate for candidate in same_number
                            if item_scraper.contract_matches(candidate, row)
                        ]
                    if len(matches) != 1:
                        visible_numbers = [candidate.get("numero") for candidate in filtered_rows[:12]]
                        raise RuntimeError(
                            f"Busca pelo Nº do Contrato encontrou {len(same_number)} linha(s) para {label}; "
                            f"após desambiguação restaram {len(matches)}. "
                            f"Números visíveis: {visible_numbers}."
                        )
                    detail = await scraper.extract_detail_and_items(context, page, matches[0])
                    confirmed, confirmation_error = item_scraper.confirm_items(detail)
                    if not confirmed:
                        detail.pop("itens_extraidos", None)
                        detail["erro_itens"] = confirmation_error
                except Exception as exc:
                    detail = {**row, "erro_extracao": f"{type(exc).__name__}: {exc}"}

                current = dict(existing) if existing is not None else {}
                current.update(row)
                # Só substitua/declare itens quando a rotina auxiliar validou
                # a tela. Em falhas, preserve qualquer item prévio confirmado.
                if detail.get("itens_extraidos") is True and isinstance(detail.get("itens"), list):
                    current.update(detail)
                    current["itens_extraidos"] = True
                    current["itens_quantidade"] = len(detail["itens"])
                    current["itens_atualizados_em"] = local_now().isoformat(timespec="seconds")
                    for err in ("erro_itens", "erro_detalhe", "erro_extracao", "erro_validacao_contrato"):
                        current.pop(err, None)
                    print(f"  OK — {len(detail['itens'])} item(ns) confirmado(s).", flush=True)
                    if existing is None:
                        added += 1
                    else:
                        updated += 1
                else:
                    current["erro_itens"] = (
                        detail.get("erro_itens") or detail.get("erro_detalhe") or
                        detail.get("erro_extracao") or detail.get("erro_validacao_contrato") or
                        "Itens não confirmados; será tentado novamente na próxima janela."
                    )
                    current["itens_tentativa_em"] = local_now().isoformat(timespec="seconds")
                    pending += 1
                    if existing is None:
                        added += 1
                    else:
                        updated += 1
                    print(f"  PENDENTE — {current['erro_itens']}", flush=True)

                if existing_index is None:
                    records.append(current)
                    lookup[key] = len(records) - 1
                else:
                    records[existing_index] = current

                write_json_atomic(args.json, payload)

                # A grade deve voltar ao filtro amplo antes da busca seguinte.
                try:
                    await item_scraper.clear_number_search(scraper, page, baseline_count)
                    print("  Filtro de Nº do Contrato limpo; grade restaurada.", flush=True)
                except Exception as reset_error:
                    raise RuntimeError(
                        f"JSON salvo para {label}, mas não consegui restaurar a grade antes "
                        f"do próximo contrato: {reset_error}"
                    ) from reset_error
        except Exception:
            if args.headed:
                print("\nA execução parou para não salvar uma leitura vazia/incompleta. "
                      "O Chrome ficará aberto para inspeção; pressione Enter para encerrar.", flush=True)
                await asyncio.to_thread(input)
            raise
        finally:
            await browser.close()

    print(
        f"Resumo: {added} novo(s), {updated} registro(s) existente(s) completado(s), "
        f"{skipped} já completo(s) ignorado(s), {ignored} da Câmara ignorado(s), "
        f"{pending} pendente(s).",
        flush=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", type=Path, default=DEFAULT_JSON,
                        help="JSON da página (padrão: dados/resultcontratos.json)")
    parser.add_argument("--lookback-days", type=positive_int, default=None,
                        help="opção antiga aceita por compatibilidade; não filtra a grade do portal")
    parser.add_argument("--headed", action="store_true", help="abre o navegador visível para depuração")
    parsed_args = parser.parse_args()
    if parsed_args.lookback_days is not None:
        print("Aviso: --lookback-days não altera a consulta; será usada a grade atual do portal.", flush=True)
    asyncio.run(run(parsed_args))
