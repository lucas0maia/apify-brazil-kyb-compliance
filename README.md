# Brazil KYB, Sanctions & ESG Due Diligence (`apify-brazil-kyb-compliance`)

Screen Brazilian companies (CNPJ) and individuals (CPF) against **6 official Brazilian government open-data compliance, sanctions, and ESG registries** in one fast, proxy-free API or MCP call:

- **Receita Federal CNPJ & QSA** (registration status, share capital, CNAE, address, corporate partners via BrasilAPI / MinhaReceita)
- **CGU CEIS** (Cadastro Nacional de Empresas Inidôneas e Suspensas — public procurement bans)
- **CGU CNEP** (Cadastro Nacional de Empresas Punidas — Brazilian Anti-Corruption Law fines)
- **CGU CEPIM** (Entidades Privadas Sem Fins Lucrativos Impedidas)
- **MTE Lista Suja do Trabalho Escravo** (Brazilian Ministry of Labor Forced Labor Dirty List)
- **IBAMA Termos de Embargo** (Environmental land/deforestation embargoes with hectares & lat/lng)

## Operation Modes

| Mode | Purpose | Charged Event |
| :--- | :--- | :--- |
| `screen_entities` (default) | Multi-registry KYB & ESG risk score (`0-100`, `CLEAN` -> `CRITICAL`) + QSA partners per CNPJ/CPF | `entity-screened` |
| `ceis_sanctions` | Search/filter CGU CEIS active sanctions by UF or keyword | `sanction-record` |
| `cnep_punishments` | Search/filter CGU CNEP corporate corruption fines | `sanction-record` |
| `cepim_impediments` | Search/filter CGU CEPIM grant impediments | `sanction-record` |
| `slave_labor_registry` | Search/filter MTE Slave Labor Dirty List | `sanction-record` |
| `ibama_embargoes` | Search/filter active IBAMA environmental embargoes | `sanction-record` |

## Example Input

```json
{
  "mode": "screen_entities",
  "documents": ["33000167000101", "11338577000124"],
  "includeReceita": true,
  "includeIbama": false
}
```

## Example Output (`screen_entities`)

```json
{
  "recordType": "KYB_SCREENING",
  "document": "11338577000124",
  "entityName": "CASSOL REPRESENTACOES E INTERMEDIACOES LTDA",
  "riskLevel": "HIGH",
  "riskScore": 50,
  "registrationStatus": "ATIVA",
  "totalSanctions": 2,
  "ceisHits": 1,
  "cnepHits": 1,
  "summary": "CEIS: ... | CNEP: Multa R$ 36,680.49"
}
```

## MCP & Standby Ready

Runs as an on-demand Apify Actor, REST endpoint (Standby), or MCP server exposing `screen_brazil_entities` and `search_brazil_sanctions`. Zero residential proxies required.
