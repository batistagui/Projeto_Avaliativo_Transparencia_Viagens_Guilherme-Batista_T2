"""
1_extrair.py
------------
Fase 1 do pipeline - Extracao e camada RAW.

O que este script faz, em ordem:
  1) Baixa o .zip do Google Drive (usando o DRIVE_FILE_ID do config.py).
  2) Extrai os 4 CSVs para a pasta data/.
  3) Le cada CSV em blocos (chunks) e carrega na tabela RAW correspondente,
     sem alterar nenhum valor (fidelidade total ao dado bruto).

Caracteristicas exigidas pelo desafio:
  - Idempotente: cada tabela RAW e TRUNCATE'ada antes da carga, entao rodar
    o script varias vezes nunca duplica registro.
  - Resiliente: cada etapa (download, extracao, carga por arquivo) roda dentro
    de um try/except, registrando o erro no console sem derrubar o pipeline
    inteiro por causa de um unico arquivo com problema.
"""

import sys
import zipfile
from pathlib import Path

import gdown
import pandas as pd

from banco import conectar, executar, inserir_em_lote
from config import (
    ARQUIVOS,
    CSV_ENCODING,
    CSV_SEPARADOR,
    DRIVE_FILE_ID,
    PASTA_DADOS,
    TAMANHO_BLOCO,
)

NOME_ZIP = "viagens_2025.zip"


# =============================================================================
# 1) DOWNLOAD
# =============================================================================
def baixar_zip_do_drive(file_id, pasta_destino):
    """
    Baixa o .zip do Google Drive para pasta_destino/NOME_ZIP.
    Se o arquivo ja existir localmente, o download e pulado (evita baixar de
    novo tudo toda vez que o pipeline roda).
    """
    pasta_destino.mkdir(parents=True, exist_ok=True)
    caminho_zip = pasta_destino / NOME_ZIP

    if caminho_zip.exists():
        print(f"[extrair] '{NOME_ZIP}' ja existe em {pasta_destino}, pulando download.")
        return caminho_zip

    if not file_id or file_id.startswith("COLE_AQUI"):
        raise RuntimeError(
            "DRIVE_FILE_ID nao foi configurado em config.py. "
            "Cole o ID do arquivo do Google Drive antes de rodar este script."
        )

    print(f"[extrair] Baixando .zip do Google Drive (id={file_id})...")
    url = f"https://drive.google.com/uc?id={file_id}"
    gdown.download(url, str(caminho_zip), quiet=False)
    print(f"[extrair] Download concluido em {caminho_zip}")
    return caminho_zip


def extrair_zip(caminho_zip, pasta_destino):
    """Extrai todo o conteudo do .zip para pasta_destino."""
    print(f"[extrair] Extraindo {caminho_zip.name}...")
    with zipfile.ZipFile(caminho_zip, "r") as z:
        z.extractall(pasta_destino)
    print("[extrair] Extracao concluida.")


# =============================================================================
# 2) LEITURA DOS CSVS (colunas na MESMA ordem do arquivo original, so
#    renomeadas para nomes de banco -- nenhum valor e alterado aqui)
# =============================================================================
COLUNAS_RAW = {
    "viagem": [
        "id_viagem", "num_proposta", "situacao", "viagem_urgente",
        "justificativa_urgencia", "cod_orgao_superior", "nome_orgao_superior",
        "cod_orgao_solicitante", "nome_orgao_solicitante", "cpf_viajante",
        "nome_viajante", "cargo", "funcao", "descricao_funcao",
        "data_inicio", "data_fim", "destinos", "motivo",
        "valor_diarias", "valor_passagens", "valor_devolucao", "valor_outros_gastos",
    ],
    "pagamento": [
        "id_viagem", "num_proposta", "cod_orgao_superior", "nome_orgao_superior",
        "cod_orgao_pagador", "nome_orgao_pagador", "cod_ug_pagadora",
        "nome_ug_pagadora", "tipo_pagamento", "valor",
    ],
    "passagem": [
        "id_viagem", "num_proposta", "meio_transporte",
        "pais_origem_ida", "uf_origem_ida", "cidade_origem_ida",
        "pais_destino_ida", "uf_destino_ida", "cidade_destino_ida",
        "pais_origem_volta", "uf_origem_volta", "cidade_origem_volta",
        "pais_destino_volta", "uf_destino_volta", "cidade_destino_volta",
        "valor_passagem", "taxa_servico", "data_emissao", "hora_emissao",
    ],
    "trecho": [
        "id_viagem", "num_proposta", "sequencia_trecho", "origem_data",
        "origem_pais", "origem_uf", "origem_cidade", "destino_data",
        "destino_pais", "destino_uf", "destino_cidade", "meio_transporte",
        "numero_diarias", "missao",
    ],
}


def ler_csv_em_blocos(caminho_csv, colunas):
    """
    Retorna um iterador de DataFrames (blocos de TAMANHO_BLOCO linhas).
    Le tudo como texto (dtype=str) e sem conversao de valores ausentes,
    porque a camada RAW deve preservar o dado exatamente como veio do CSV.
    """
    return pd.read_csv(
        caminho_csv,
        sep=CSV_SEPARADOR,
        encoding=CSV_ENCODING,
        header=0,
        names=colunas,
        dtype=str,
        keep_default_na=False,
        na_filter=False,
        chunksize=TAMANHO_BLOCO,
    )


# =============================================================================
# 3) CARGA NA CAMADA RAW
# =============================================================================
def carregar_tabela_raw(conexao, tabela, colunas, caminho_csv):
    """
    TRUNCATE na tabela (idempotencia) e carga em blocos do CSV correspondente.
    """
    executar(conexao, f"TRUNCATE TABLE {tabela}")

    placeholders = ", ".join(["%s"] * len(colunas))
    colunas_sql = ", ".join(colunas)
    sql_insert = f"INSERT INTO {tabela} ({colunas_sql}) VALUES ({placeholders})"

    total_linhas = 0
    for bloco in ler_csv_em_blocos(caminho_csv, colunas):
        linhas = [tuple(linha) for linha in bloco.itertuples(index=False, name=None)]
        inserir_em_lote(conexao, sql_insert, linhas)
        total_linhas += len(linhas)
        print(f"[extrair] {tabela}: +{len(linhas)} linhas (total: {total_linhas})")

    print(f"[extrair] {tabela}: carga concluida ({total_linhas} linhas).")


# =============================================================================
# MAIN
# =============================================================================
def main():
    try:
        caminho_zip = baixar_zip_do_drive(DRIVE_FILE_ID, PASTA_DADOS)
        extrair_zip(caminho_zip, PASTA_DADOS)
    except Exception as erro:
        print(f"[extrair] ERRO no download/extracao: {erro}", file=sys.stderr)
        return

    try:
        conexao = conectar()
    except Exception as erro:
        print(f"[extrair] ERRO ao conectar no banco: {erro}", file=sys.stderr)
        return

    try:
        for chave, info in ARQUIVOS.items():
            caminho_csv = PASTA_DADOS / info["csv"]
            tabela = info["tabela_raw"]
            colunas = COLUNAS_RAW[chave]
            try:
                if not caminho_csv.exists():
                    raise FileNotFoundError(f"Arquivo nao encontrado: {caminho_csv}")
                carregar_tabela_raw(conexao, tabela, colunas, caminho_csv)
            except Exception as erro:
                # Um arquivo com problema nao derruba a carga dos demais.
                print(f"[extrair] ERRO ao carregar '{tabela}': {erro}", file=sys.stderr)
    finally:
        conexao.close()

    print("[extrair] Pipeline de extracao finalizado.")


if __name__ == "__main__":
    main()
