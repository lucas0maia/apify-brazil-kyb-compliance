"""Client and risk-scoring engine for Brazilian public KYB, ESG, and sanctions datasets."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import re
import time
import urllib.error
import urllib.request
import zipfile
from typing import Any

USER_AGENT = "Mozilla/5.0 (compatible; ApifyBrazilKYB/1.0; +https://apify.com/brasil-api-lcm)"

CGU_PAGE_TEMPLATE = "https://portaldatransparencia.gov.br/download-de-dados/{kind}"
CGU_ZIP_TEMPLATE = "https://portaldatransparencia.gov.br/download-de-dados/{kind}/{ymd}"
MTE_SLAVE_LABOR_URL = (
    "https://www.gov.br/trabalho-e-emprego/pt-br/assuntos/inspecao-do-trabalho/"
    "areas-de-atuacao/cadastro_de_empregadores.csv"
)
IBAMA_EMBARGO_URL = (
    "https://dadosabertos.ibama.gov.br/dados/SIFISC/termo_embargo/"
    "termo_embargo/termo_embargo_csv.zip"
)
BRASILAPI_CNPJ_URL = "https://brasilapi.com.br/api/cnpj/v1/{cnpj}"
MINHARECEITA_CNPJ_URL = "https://minhareceita.org/{cnpj}"

_CACHE_TTL_SECS = 1800
_CACHE: dict[str, tuple[float, Any]] = {}


def normalize_document(value: str | None) -> str:
    """Strip non-digits from a Brazilian CNPJ or CPF string."""
    if not value:
        return ""
    return re.sub(r"\D+", "", str(value))


def parse_ptbr_number(value: str | None) -> float | None:
    """Parse PT-BR decimal number (e.g. '36680,49' or '1.234,56') into float."""
    if not value:
        return None
    cleaned = str(value).strip()
    if not cleaned:
        return None
    if "," in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _matches_filters(
    row_text: str,
    query: str,
    row_uf: str = "",
    uf_filter: str = "",
) -> bool:
    if uf_filter and row_uf.upper() != uf_filter.upper():
        return False
    if query and query.lower() not in row_text.lower():
        return False
    return True


def _row_matches_doc(row_doc: str, target_doc: str) -> bool:
    if not row_doc or not target_doc:
        return False
    if row_doc == target_doc:
        return True
    # Match root 8-digit CNPJ against full 14-digit branch CNPJs when both are CNPJs
    if len(target_doc) == 14 and len(row_doc) == 14 and row_doc[:8] == target_doc[:8]:
        return True
    return False


def parse_cgu_date_from_html(html: str) -> str | None:
    """Extract latest YYYYMMDD date published inside CGU download-de-dados HTML."""
    match = re.search(
        r'arquivos\.push\(\{\s*"ano"\s*:\s*"(\d{4})"\s*,\s*"mes"\s*:\s*"(\d{2})"\s*,\s*"dia"\s*:\s*"(\d{2})"',
        html,
    )
    if not match:
        return None
    return f"{match.group(1)}{match.group(2)}{match.group(3)}"


def parse_cgu_sanctions_csv(
    csv_text: str,
    registry: str,
    source_url: str,
    query: str = "",
    uf: str = "",
    max_results: int = 500000,
) -> list[dict[str, Any]]:
    """Parse CGU CEIS, CNEP, or CEPIM semicolon-delimited CSV text into normalized records."""
    reader = csv.DictReader(io.StringIO(csv_text), delimiter=";")
    records: list[dict[str, Any]] = []
    reg_upper = registry.upper()

    for row in reader:
        if reg_upper == "CEPIM":
            doc = normalize_document(row.get("CNPJ ENTIDADE"))
            name = (row.get("NOME ENTIDADE") or "").strip()
            category = "Convenio Impediment (CEPIM)"
            reason = (row.get("MOTIVO DO IMPEDIMENTO") or "").strip()
            agency = (row.get("ÓRGÃO CONCEDENTE") or "").strip()
            row_uf = ""
            start_date = ""
            end_date = ""
            fine_brl = None
            process_no = (row.get("NÚMERO CONVÊNIO") or "").strip()
        else:
            doc = normalize_document(row.get("CPF OU CNPJ DO SANCIONADO"))
            name = (
                row.get("RAZÃO SOCIAL - CADASTRO RECEITA")
                or row.get("NOME DO SANCIONADO")
                or row.get("NOME INFORMADO PELO ÓRGÃO SANCIONADOR")
                or ""
            ).strip()
            category = (row.get("CATEGORIA DA SANÇÃO") or "").strip()
            reason = (row.get("FUNDAMENTAÇÃO LEGAL") or row.get("OBSERVAÇÕES") or "").strip()
            agency = (row.get("ÓRGÃO SANCIONADOR") or "").strip()
            row_uf = (row.get("UF ÓRGÃO SANCIONADOR") or "").strip().upper()
            start_date = (row.get("DATA INÍCIO SANÇÃO") or "").strip()
            end_date = (row.get("DATA FINAL SANÇÃO") or "").strip()
            fine_brl = parse_ptbr_number(row.get("VALOR DA MULTA"))
            process_no = (row.get("NÚMERO DO PROCESSO") or "").strip()

        blob = f"{doc} {name} {category} {reason} {agency} {process_no}"
        if not _matches_filters(blob, query=query, row_uf=row_uf, uf_filter=uf):
            continue

        records.append(
            {
                "recordType": reg_upper,
                "document": doc,
                "entityName": name,
                "riskLevel": "HIGH" if reg_upper in ("CEIS", "CNEP") else "MEDIUM",
                "riskScore": 45 if reg_upper == "CNEP" else (40 if reg_upper == "CEIS" else 20),
                "registrationStatus": category,
                "totalSanctions": 1,
                "uf": row_uf,
                "agency": agency,
                "processNumber": process_no,
                "startDate": start_date,
                "endDate": end_date,
                "fineBRL": fine_brl,
                "summary": f"{reg_upper}: {category}" + (f" | {agency}" if agency else "") + (f" | {reason[:140]}" if reason else ""),
                "sourceUrl": source_url,
            }
        )
        if len(records) >= max_results:
            break

    return records


def parse_slave_labor_csv(
    csv_text: str,
    query: str = "",
    uf: str = "",
    max_results: int = 50000,
) -> list[dict[str, Any]]:
    """Parse MTE Cadastro de Empregadores (Lista Suja Trabalho Escravo) CSV."""
    reader = csv.DictReader(io.StringIO(csv_text), delimiter=";")
    records: list[dict[str, Any]] = []

    for row in reader:
        doc = normalize_document(row.get("CNPJ/CPF"))
        name = (row.get("Empregador") or "").strip()
        row_uf = (row.get("UF") or "").strip().upper()
        workers = row.get("Trabalhadores envolvidos") or ""
        estab = (row.get("Estabelecimento") or "").strip()
        year = (row.get("Ano da ação fiscal") or "").strip()
        included = (row.get("Inclusão no Cadastro de Empregadores") or "").strip()
        cnae = (row.get("CNAE") or "").strip()

        blob = f"{doc} {name} {estab} {cnae} {year}"
        if not _matches_filters(blob, query=query, row_uf=row_uf, uf_filter=uf):
            continue

        records.append(
            {
                "recordType": "SLAVE_LABOR",
                "document": doc,
                "entityName": name,
                "riskLevel": "CRITICAL",
                "riskScore": 60,
                "registrationStatus": f"Active Listed ({year})",
                "totalSanctions": 1,
                "uf": row_uf,
                "agency": "MTE / SIT",
                "workersAffected": int(workers) if str(workers).isdigit() else None,
                "startDate": included,
                "endDate": "",
                "cnae": cnae,
                "summary": f"MTE Lista Suja ({year}): {workers} workers affected | {estab}",
                "sourceUrl": MTE_SLAVE_LABOR_URL,
            }
        )
        if len(records) >= max_results:
            break

    return records


def parse_ibama_embargo_stream(
    text_stream: io.TextIOBase,
    query: str = "",
    uf: str = "",
    target_docs: set[str] | None = None,
    max_results: int = 50000,
) -> list[dict[str, Any]]:
    """Stream-parse IBAMA termo_embargo.csv without inflating full 47MB ZIP into string."""
    reader = csv.DictReader(text_stream, delimiter=";")
    records: list[dict[str, Any]] = []
    target_roots = {d[:8] for d in target_docs if len(d) == 14} if target_docs else set()

    for row in reader:
        if (row.get("SIT_CANCELADO") or "").strip().upper() == "S":
            continue
        # Skip already lifted embargoes unless user specifically wants historical ones
        if (row.get("SIT_DESEMBARGO") or "").strip():
            continue

        doc = normalize_document(row.get("CPF_CNPJ_EMBARGADO"))
        if target_docs is not None:
            if not doc:
                continue
            if doc not in target_docs and not (len(doc) == 14 and doc[:8] in target_roots):
                continue

        name = (row.get("NOME_EMBARGADO") or "").strip()
        row_uf = (row.get("UF") or "").strip().upper()
        mun = (row.get("MUNICIPIO") or "").strip()
        area_ha = parse_ptbr_number(row.get("QTD_AREA_EMBARGADA"))
        prop_name = (row.get("NOME_IMOVEL") or "").strip()
        desc = (row.get("DES_TAD") or "").strip()
        num_tad = (row.get("NUM_TAD") or "").strip()
        emb_date = (row.get("DAT_EMBARGO") or "").strip()

        blob = f"{doc} {name} {mun} {prop_name} {desc} {num_tad}"
        if not _matches_filters(blob, query=query, row_uf=row_uf, uf_filter=uf):
            continue

        records.append(
            {
                "recordType": "IBAMA_EMBARGO",
                "document": doc,
                "entityName": name,
                "riskLevel": "MEDIUM",
                "riskScore": 30,
                "registrationStatus": (row.get("DES_STATUS_FORMULARIO") or "Embargado").strip(),
                "totalSanctions": 1,
                "uf": row_uf,
                "municipality": mun,
                "agency": "IBAMA",
                "processNumber": (row.get("NUM_PROCESSO") or "").strip(),
                "embargoedHectares": area_ha,
                "startDate": emb_date,
                "latitude": parse_ptbr_number(row.get("NUM_LATITUDE_TAD")),
                "longitude": parse_ptbr_number(row.get("NUM_LONGITUDE_TAD")),
                "summary": (
                    f"IBAMA Embargo #{num_tad} ({mun}/{row_uf})"
                    + (f" | {area_ha:.2f} ha" if area_ha is not None else "")
                    + (f" | {prop_name}" if prop_name else "")
                ),
                "sourceUrl": IBAMA_EMBARGO_URL,
            }
        )
        if len(records) >= max_results:
            break

    return records


def compute_risk_verdict(
    sanctions: list[dict[str, Any]],
    receita: dict[str, Any] | None,
) -> tuple[int, str]:
    """Calculate consolidated 0-100 risk score and categorical risk level."""
    score = 0
    seen_types = {str(item.get("recordType", "")).upper() for item in sanctions}

    if "SLAVE_LABOR" in seen_types:
        score += 60
    if "CNEP" in seen_types:
        score += 45
    if "CEIS" in seen_types:
        score += 40
    if "IBAMA_EMBARGO" in seen_types:
        score += 30
    if "CEPIM" in seen_types:
        score += 20

    # Extra penalty for multiple hits across sanctions lists
    if len(sanctions) > 1:
        score += min(15, (len(sanctions) - 1) * 5)

    if receita:
        status = str(receita.get("descricao_situacao_cadastral") or "").upper()
        if status and status != "ATIVA":
            score += 20

    score = min(100, score)
    if score == 0:
        level = "CLEAN"
    elif score < 30:
        level = "LOW"
    elif score < 50:
        level = "MEDIUM"
    elif score < 75:
        level = "HIGH"
    else:
        level = "CRITICAL"
    return score, level


def _http_get_bytes(url: str, timeout: int) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _cached_fetch_bytes(cache_key: str, url: str, timeout: int) -> bytes:
    now = time.monotonic()
    hit = _CACHE.get(cache_key)
    if hit and (now - hit[0]) < _CACHE_TTL_SECS:
        return hit[1]
    data = _http_get_bytes(url, timeout=timeout)
    _CACHE[cache_key] = (now, data)
    return data


def fetch_cgu_registry(
    kind: str,
    query: str = "",
    uf: str = "",
    max_results: int = 500000,
    timeout: int = 30,
) -> list[dict[str, Any]]:
    """Fetch CGU CEIS/CNEP/CEPIM ZIP dynamically via latest Portal da Transparencia date."""
    page_url = CGU_PAGE_TEMPLATE.format(kind=kind)
    html = _cached_fetch_bytes(f"html:{kind}", page_url, timeout).decode("utf-8", errors="ignore")
    ymd = parse_cgu_date_from_html(html)
    if not ymd:
        raise RuntimeError(f"Could not resolve latest download date from {page_url}")

    zip_url = CGU_ZIP_TEMPLATE.format(kind=kind, ymd=ymd)
    zbytes = _cached_fetch_bytes(f"zip:{kind}:{ymd}", zip_url, timeout)
    with zipfile.ZipFile(io.BytesIO(zbytes)) as zf:
        csv_name = zf.namelist()[0]
        csv_text = zf.read(csv_name).decode("latin-1", errors="ignore")
    return parse_cgu_sanctions_csv(
        csv_text=csv_text,
        registry=kind.upper(),
        source_url=zip_url,
        query=query,
        uf=uf,
        max_results=max_results,
    )


def fetch_slave_labor_registry(
    query: str = "",
    uf: str = "",
    max_results: int = 50000,
    timeout: int = 30,
) -> list[dict[str, Any]]:
    """Fetch MTE Cadastro de Empregadores (Lista Suja do Trabalho Escravo)."""
    raw = _cached_fetch_bytes("csv:mte_slave_labor", MTE_SLAVE_LABOR_URL, timeout)
    csv_text = raw.decode("latin-1", errors="ignore")
    return parse_slave_labor_csv(csv_text, query=query, uf=uf, max_results=max_results)


def fetch_ibama_registry(
    query: str = "",
    uf: str = "",
    target_docs: set[str] | None = None,
    max_results: int = 5000,
    timeout: int = 45,
) -> list[dict[str, Any]]:
    """Fetch and stream-scan IBAMA Environmental Embargoes ZIP."""
    zbytes = _cached_fetch_bytes("zip:ibama_embargo", IBAMA_EMBARGO_URL, timeout)
    with zipfile.ZipFile(io.BytesIO(zbytes)) as zf:
        csv_name = zf.namelist()[0]
        with zf.open(csv_name) as bin_stream:
            txt_stream = io.TextIOWrapper(bin_stream, encoding="utf-8", errors="ignore")
            return parse_ibama_embargo_stream(
                txt_stream,
                query=query,
                uf=uf,
                target_docs=target_docs,
                max_results=max_results,
            )


def fetch_receita_cnpj(cnpj: str, timeout: int = 15) -> dict[str, Any] | None:
    """Fetch Receita Federal registration & QSA partners via BrasilAPI with MinhaReceita fallback."""
    digits = normalize_document(cnpj)
    if len(digits) != 14:
        return None
    cache_key = f"cnpj:{digits}"
    now = time.monotonic()
    hit = _CACHE.get(cache_key)
    if hit and (now - hit[0]) < _CACHE_TTL_SECS:
        return hit[1]

    for template in (BRASILAPI_CNPJ_URL, MINHARECEITA_CNPJ_URL):
        try:
            raw = _http_get_bytes(template.format(cnpj=digits), timeout=timeout)
            data = json.loads(raw.decode("utf-8"))
            if isinstance(data, dict) and data.get("razao_social"):
                _CACHE[cache_key] = (now, data)
                return data
        except (urllib.error.URLError, TimeoutError, ValueError):
            continue
    return None


async def screen_entities_async(
    documents: list[str],
    query: str = "",
    include_receita: bool = True,
    include_ibama: bool = False,
    timeout: int = 30,
) -> list[dict[str, Any]]:
    """Screen multiple CNPJs/CPFs concurrently across Receita Federal, CGU, MTE, and optional IBAMA."""
    clean_docs = [normalize_document(d) for d in documents if normalize_document(d)]
    if not clean_docs and not query.strip():
        raise ValueError("Provide at least one CNPJ/CPF in 'documents' or a non-empty 'query'.")

    loop = asyncio.get_running_loop()
    tasks = [
        loop.run_in_executor(None, fetch_cgu_registry, "ceis", "", "", 500000, timeout),
        loop.run_in_executor(None, fetch_cgu_registry, "cnep", "", "", 500000, timeout),
        loop.run_in_executor(None, fetch_cgu_registry, "cepim", "", "", 500000, timeout),
        loop.run_in_executor(None, fetch_slave_labor_registry, "", "", 50000, timeout),
    ]
    if include_ibama:
        tasks.append(
            loop.run_in_executor(
                None,
                fetch_ibama_registry,
                "",
                "",
                set(clean_docs) if clean_docs else None,
                5000,
                max(timeout, 45),
            )
        )

    fetched_lists = await asyncio.gather(*tasks, return_exceptions=True)
    source_errors: list[str] = []
    combined_sanctions: list[dict[str, Any]] = []
    for idx, res in enumerate(fetched_lists):
        if isinstance(res, Exception):
            source_errors.append(f"source_{idx}: {res}")
        else:
            combined_sanctions.extend(res)

    if len(source_errors) == len(fetched_lists):
        raise RuntimeError(f"All compliance data sources failed: {'; '.join(source_errors)}")

    # Name-only search when user supplies `query` without `documents`
    if not clean_docs:
        q_lower = query.strip().lower()
        matched = [
            s for s in combined_sanctions
            if q_lower in f"{s.get('entityName', '')} {s.get('document', '')} {s.get('summary', '')}".lower()
        ]
        return matched[:100]

    receita_results: dict[str, dict[str, Any] | None] = {}
    if include_receita:
        cnpj_targets = [d for d in clean_docs if len(d) == 14]
        rec_fetched = await asyncio.gather(
            *[loop.run_in_executor(None, fetch_receita_cnpj, d, min(timeout, 15)) for d in cnpj_targets]
        )
        receita_results = dict(zip(cnpj_targets, rec_fetched))

    screened_rows: list[dict[str, Any]] = []
    for doc in clean_docs:
        hits = [s for s in combined_sanctions if _row_matches_doc(s.get("document", ""), doc)]
        receita = receita_results.get(doc)
        score, level = compute_risk_verdict(hits, receita)

        ceis_hits = [h for h in hits if h["recordType"] == "CEIS"]
        cnep_hits = [h for h in hits if h["recordType"] == "CNEP"]
        cepim_hits = [h for h in hits if h["recordType"] == "CEPIM"]
        mte_hits = [h for h in hits if h["recordType"] == "SLAVE_LABOR"]
        ibama_hits = [h for h in hits if h["recordType"] == "IBAMA_EMBARGO"]

        entity_name = (
            (receita or {}).get("razao_social")
            or (hits[0]["entityName"] if hits else "")
            or f"Document {doc}"
        )
        reg_status = (receita or {}).get("descricao_situacao_cadastral") or (
            "CPF / Not Applicable" if len(doc) == 11 else "Unavailable"
        )
        qsa = [
            {
                "name": partner.get("nome_socio"),
                "role": partner.get("qualificacao_socio"),
                "document": partner.get("cnpj_cpf_do_socio"),
            }
            for partner in ((receita or {}).get("qsa") or [])
        ]

        findings = "; ".join(h["summary"] for h in hits[:5]) if hits else "No sanctions or ESG alerts found."
        screened_rows.append(
            {
                "recordType": "KYB_SCREENING",
                "document": doc,
                "entityName": entity_name,
                "tradeName": (receita or {}).get("nome_fantasia") or "",
                "riskLevel": level,
                "riskScore": score,
                "registrationStatus": reg_status,
                "uf": (receita or {}).get("uf") or (hits[0].get("uf") if hits else ""),
                "municipality": (receita or {}).get("municipio") or "",
                "cnae": (receita or {}).get("cnae_fiscal_descricao") or "",
                "shareCapitalBRL": (receita or {}).get("capital_social"),
                "openedAt": (receita or {}).get("data_inicio_atividade") or "",
                "partnersCount": len(qsa),
                "partnersQSA": qsa,
                "totalSanctions": len(hits),
                "ceisHits": len(ceis_hits),
                "cnepHits": len(cnep_hits),
                "cepimHits": len(cepim_hits),
                "slaveLaborHits": len(mte_hits),
                "ibamaEmbargoHits": len(ibama_hits),
                "sanctions": hits,
                "summary": findings,
                "sourcesChecked": (
                    ["CGU_CEIS", "CGU_CNEP", "CGU_CEPIM", "MTE_SLAVE_LABOR"]
                    + (["IBAMA_EMBARGO"] if include_ibama else [])
                    + (["RECEITA_CNPJ_QSA"] if include_receita and len(doc) == 14 else [])
                ),
                "sourceWarnings": source_errors,
                "sourceUrl": f"https://brasilapi.com.br/api/cnpj/v1/{doc}" if len(doc) == 14 else MTE_SLAVE_LABOR_URL,
            }
        )

    return screened_rows


async def run_kyb_query(actor_input: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    """Execute selected KYB compliance or registry mode and return (records, ppe_event_name)."""
    mode = str(actor_input.get("mode") or "screen_entities").strip()
    documents = list(actor_input.get("documents") or [])
    query = str(actor_input.get("query") or "").strip()
    uf = str(actor_input.get("uf") or "").strip().upper()
    include_receita = bool(actor_input.get("includeReceita", True))
    include_ibama = bool(actor_input.get("includeIbama", False))
    max_results = max(1, min(5000, int(actor_input.get("maxResults") or 50)))
    timeout = max(5, min(120, int(actor_input.get("timeoutSecs") or 30)))

    loop = asyncio.get_running_loop()

    if mode == "screen_entities":
        rows = await screen_entities_async(
            documents=documents,
            query=query,
            include_receita=include_receita,
            include_ibama=include_ibama,
            timeout=timeout,
        )
        return rows[:max_results], "entity-screened"

    if mode == "ceis_sanctions":
        rows = await loop.run_in_executor(None, fetch_cgu_registry, "ceis", query, uf, max_results, timeout)
        return rows, "sanction-record"
    if mode == "cnep_punishments":
        rows = await loop.run_in_executor(None, fetch_cgu_registry, "cnep", query, uf, max_results, timeout)
        return rows, "sanction-record"
    if mode == "cepim_impediments":
        rows = await loop.run_in_executor(None, fetch_cgu_registry, "cepim", query, uf, max_results, timeout)
        return rows, "sanction-record"
    if mode == "slave_labor_registry":
        rows = await loop.run_in_executor(None, fetch_slave_labor_registry, query, uf, max_results, timeout)
        return rows, "sanction-record"
    if mode == "ibama_embargoes":
        rows = await loop.run_in_executor(
            None, fetch_ibama_registry, query, uf, None, max_results, max(timeout, 45)
        )
        return rows, "sanction-record"

    raise ValueError(f"Unsupported mode: {mode}")
