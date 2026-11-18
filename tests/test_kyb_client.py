"""Unit tests for Brazil KYB & ESG Sanctions parser and risk scoring."""

from __future__ import annotations

import io
import unittest

from src.kyb_client import (
    compute_risk_verdict,
    normalize_document,
    parse_cgu_date_from_html,
    parse_cgu_sanctions_csv,
    parse_ibama_embargo_stream,
    parse_slave_labor_csv,
)


class KybClientTest(unittest.TestCase):
    def test_normalize_and_parse_cgu_date(self) -> None:
        self.assertEqual(normalize_document("11.338.577/0001-24"), "11338577000124")
        html = '<script>arquivos.push({"ano" : "2026", "mes" : "11", "dia" : "17", "origem" : "CEIS"})</script>'
        self.assertEqual(parse_cgu_date_from_html(html), "20261117")

    def test_parse_cnep_and_cepim_csv(self) -> None:
        cnep_csv = (
            '"CADASTRO";"CÓDIGO DA SANÇÃO";"TIPO DE PESSOA";"CPF OU CNPJ DO SANCIONADO";'
            '"NOME DO SANCIONADO";"NOME INFORMADO PELO ÓRGÃO SANCIONADOR";"RAZÃO SOCIAL - CADASTRO RECEITA";'
            '"NÚMERO DO PROCESSO";"CATEGORIA DA SANÇÃO";"VALOR DA MULTA";"DATA INÍCIO SANÇÃO";'
            '"DATA FINAL SANÇÃO";"ÓRGÃO SANCIONADOR";"UF ÓRGÃO SANCIONADOR";"FUNDAMENTAÇÃO LEGAL"\n'
            '"CNEP";"138362";"J";"11338577000124";"CASSOL LTDA";"CASSOL LTDA";"CASSOL LTDA";'
            '"PAR-001";"Multa";"36680,49";"01/01/2025";"";"CGU";"PB";"Lei 12.846"\n'
        )
        rows = parse_cgu_sanctions_csv(cnep_csv, "CNEP", "https://example.org")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["document"], "11338577000124")
        self.assertAlmostEqual(rows[0]["fineBRL"], 36680.49)
        self.assertEqual(rows[0]["uf"], "PB")

        cepim_csv = (
            '"CNPJ ENTIDADE";"NOME ENTIDADE";"NÚMERO CONVÊNIO";"ÓRGÃO CONCEDENTE";"MOTIVO DO IMPEDIMENTO"\n'
            '"04410260000196";"CENTRO SOCIAL ALIANCA";"547474";"Ministerio da Saude";"TOMADA DE CONTAS ESPECIAL"\n'
        )
        cepim_rows = parse_cgu_sanctions_csv(cepim_csv, "CEPIM", "https://example.org")
        self.assertEqual(len(cepim_rows), 1)
        self.assertEqual(cepim_rows[0]["document"], "04410260000196")

    def test_parse_slave_labor_and_ibama(self) -> None:
        mte_csv = (
            "ID;Ano da ação fiscal;UF;Empregador;CNPJ/CPF;Estabelecimento;Trabalhadores envolvidos;"
            "CNAE;Decisão administrativa de procedência;Inclusão no Cadastro de Empregadores\n"
            "1;2024;SP;FAZENDA TESTE;41.297.068/0001-61;CENTRO, IGARATA/SP;5;4399-1/03;15/05/2025;06/10/2025\n"
        )
        mte_rows = parse_slave_labor_csv(mte_csv)
        self.assertEqual(len(mte_rows), 1)
        self.assertEqual(mte_rows[0]["riskLevel"], "CRITICAL")
        self.assertEqual(mte_rows[0]["workersAffected"], 5)

        ibama_csv = (
            "SEQ_TAD;DES_STATUS_FORMULARIO;SIT_CANCELADO;NUM_TAD;DAT_EMBARGO;NOME_EMBARGADO;CPF_CNPJ_EMBARGADO;"
            "NUM_PROCESSO;DES_TAD;MUNICIPIO;UF;QTD_AREA_EMBARGADA;NOME_IMOVEL;SIT_DESEMBARGO;NUM_LATITUDE_TAD;NUM_LONGITUDE_TAD\n"
            "1;Lavrado;N;2QJ;2025-09-25;AGRO TESTE;12.345.678/0001-90;02007;Desmatamento;ALTAMIRA;PA;71,99;FAZENDA BOA;;-3,2;-52,2\n"
            "2;Lavrado;S;2QK;2025-09-25;CANCELADA;99.999.999/0001-99;02008;Cancelada;ALTAMIRA;PA;10,00;FAZENDA X;;0;0\n"
        )
        ibama_rows = parse_ibama_embargo_stream(io.StringIO(ibama_csv), target_docs={"12345678000190"})
        self.assertEqual(len(ibama_rows), 1)
        self.assertAlmostEqual(ibama_rows[0]["embargoedHectares"], 71.99)

    def test_compute_risk_verdict(self) -> None:
        score_clean, level_clean = compute_risk_verdict([], {"descricao_situacao_cadastral": "ATIVA"})
        self.assertEqual((score_clean, level_clean), (0, "CLEAN"))

        score_crit, level_crit = compute_risk_verdict(
            [{"recordType": "SLAVE_LABOR"}, {"recordType": "CEIS"}],
            {"descricao_situacao_cadastral": "INAPTA"},
        )
        self.assertEqual(score_crit, 100)
        self.assertEqual(level_crit, "CRITICAL")


if __name__ == "__main__":
    unittest.main()
