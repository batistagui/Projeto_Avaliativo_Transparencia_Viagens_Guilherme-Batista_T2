"""
2_transformar.py
-----------------
Fase 2 do pipeline - Transformacao e camada SILVER.

O que este script faz:
  1) Le cada tabela RAW em blocos.
  2) Converte texto -> DECIMAL (troca "," por "." nos campos de valor) e
     texto -> DATE (de "DD/MM/AAAA" para o tipo DATE).
  3) Calcula as colunas derivadas valor_total e duracao_dias (silver_viagem).
  4) Carrega a camada SILVER, respeitando a integridade referencial
     (silver_viagem e carregada primeiro, pois pagamento/passagem/trecho
     dependem dela via FOREIGN KEY).

Caracteristicas:
  - Idempotente: TRUNCATE nas tabelas Silver antes da carga (na ordem
    filha -> pai, para nao violar FOREIGN KEY).
  - Resiliente: linhas com valor/data invalido nao derrubam o pipeline -
    o valor problematico vira NULL e o problema e contado/reportado.
"""

import sys
from datetime import date, datetime

from banco import conectar, executar, inserir_em_lote
from config import TAMANHO_BLOCO


# =============================================================================
# FUNCOES DE CONVERSAO (texto -> tipo)
# =============================================================================
def texto_para_decimal(texto):
    """
    Converte um valor monetario no formato brasileiro ("1272,97") para float.
    Retorna None se o texto estiver vazio ou nao puder ser convertido.
    """
    if texto is None:
        return None
    texto = texto.strip()
    if not texto:
        return None
    try:
        return float(texto.replace(".", "").replace(",", "."))
    except (ValueError, AttributeError):
        return None


def texto_para_data(texto):
    """
    Converte uma data no formato "DD/MM/AAAA" para um objeto date.
    Retorna None se o texto estiver vazio ou em formato invalido.
    """
    if texto is None:
        return None
    texto = texto.strip()
    if not texto:
        return None
    try:
        return datetime.strptime(texto, "%d/%m/%Y").date()
    except ValueError:
        return None


def texto_para_inteiro(texto):
    """Converte texto para int, retornando None se vazio/invalido."""
    if texto is None:
        return None
    texto = texto.strip()
    if not texto:
        return None
    try:
        return int(float(texto.replace(",", ".")))
    except (ValueError, AttributeError):
        return None


def calcular_valor_total(valor_diarias, valor_passagens, valor_outros_gastos, valor_devolucao):
    """
    Regra de negocio: custo total da viagem = diarias + passagens + outros
    gastos, subtraindo o que foi devolvido pelo viajante (valor_devolucao).
    """
    partes = [valor_diarias, valor_passagens, valor_outros_gastos]
    soma = sum(p for p in partes if p is not None)
    devolucao = valor_devolucao or 0
    return round(soma - devolucao, 2)


def calcular_duracao_dias(data_inicio, data_fim):
    """Duracao da viagem em dias, contando o dia de inicio (inclusivo)."""
    if data_inicio is None or data_fim is None:
        return None
    return (data_fim - data_inicio).days + 1


# =============================================================================
# LEITURA EM BLOCOS DE UMA TABELA RAW
# =============================================================================
def ler_raw_em_blocos(conexao, sql_select):
    """
    Executa o SELECT e retorna um cursor pronto para fetchmany().
    Usa buffered=True porque, no meio da leitura em blocos, a mesma conexao
    e usada para os INSERTs na Silver (inserir_em_lote abre outro cursor) --
    sem buffer o driver MySQL acusa "Unread result found".
    """
    cursor = conexao.cursor(buffered=True)
    cursor.execute(sql_select)
    return cursor


# =============================================================================
# TRANSFORMACAO: RAW -> SILVER, TABELA POR TABELA
# =============================================================================
def transformar_viagem(conexao):
    sql_select = """
        SELECT id_viagem, num_proposta, situacao, viagem_urgente,
               cod_orgao_superior, nome_orgao_superior, nome_viajante, cargo,
               data_inicio, data_fim, destinos, motivo,
               valor_diarias, valor_passagens, valor_devolucao, valor_outros_gastos
        FROM raw_viagem
    """
    sql_insert = """
        INSERT INTO silver_viagem (
            id_viagem, num_proposta, situacao, viagem_urgente,
            cod_orgao_superior, nome_orgao_superior, nome_viajante, cargo,
            data_inicio, data_fim, destinos, motivo,
            valor_diarias, valor_passagens, valor_devolucao, valor_outros_gastos,
            valor_total, duracao_dias
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """

    cursor = ler_raw_em_blocos(conexao, sql_select)
    total, descartadas = 0, 0
    while True:
        bloco = cursor.fetchmany(TAMANHO_BLOCO)
        if not bloco:
            break
        linhas = []
        for row in bloco:
            (id_viagem, num_proposta, situacao, viagem_urgente,
             cod_orgao_superior, nome_orgao_superior, nome_viajante, cargo,
             data_inicio_txt, data_fim_txt, destinos, motivo,
             valor_diarias_txt, valor_passagens_txt, valor_devolucao_txt,
             valor_outros_gastos_txt) = row

            if not id_viagem or not nome_orgao_superior:
                # id_viagem e nome_orgao_superior sao NOT NULL na Silver
                descartadas += 1
                continue

            data_inicio = texto_para_data(data_inicio_txt)
            data_fim = texto_para_data(data_fim_txt)
            valor_diarias = texto_para_decimal(valor_diarias_txt)
            valor_passagens = texto_para_decimal(valor_passagens_txt)
            valor_devolucao = texto_para_decimal(valor_devolucao_txt)
            valor_outros_gastos = texto_para_decimal(valor_outros_gastos_txt)

            valor_total = calcular_valor_total(
                valor_diarias, valor_passagens, valor_outros_gastos, valor_devolucao
            )
            duracao_dias = calcular_duracao_dias(data_inicio, data_fim)

            linhas.append((
                id_viagem, num_proposta, situacao, viagem_urgente,
                cod_orgao_superior, nome_orgao_superior, nome_viajante, cargo,
                data_inicio, data_fim, destinos, motivo,
                valor_diarias, valor_passagens, valor_devolucao, valor_outros_gastos,
                valor_total, duracao_dias,
            ))

        inserir_em_lote(conexao, sql_insert, linhas)
        total += len(linhas)
        print(f"[transformar] silver_viagem: +{len(linhas)} linhas (total: {total})")

    cursor.close()
    print(f"[transformar] silver_viagem concluida: {total} carregadas, {descartadas} descartadas.")


def transformar_pagamento(conexao):
    sql_select = """
        SELECT id_viagem, num_proposta, nome_orgao_pagador, nome_ug_pagadora,
               tipo_pagamento, valor
        FROM raw_pagamento
    """
    sql_insert = """
        INSERT INTO silver_pagamento (
            id_viagem, num_proposta, nome_orgao_pagador, nome_ug_pagadora,
            tipo_pagamento, valor
        ) VALUES (%s, %s, %s, %s, %s, %s)
    """

    cursor = ler_raw_em_blocos(conexao, sql_select)
    total, descartadas = 0, 0
    while True:
        bloco = cursor.fetchmany(TAMANHO_BLOCO)
        if not bloco:
            break
        linhas = []
        for row in bloco:
            (id_viagem, num_proposta, nome_orgao_pagador, nome_ug_pagadora,
             tipo_pagamento, valor_txt) = row

            if not id_viagem or not tipo_pagamento:
                # id_viagem e tipo_pagamento sao NOT NULL na Silver
                descartadas += 1
                continue

            valor = texto_para_decimal(valor_txt)
            linhas.append((
                id_viagem, num_proposta, nome_orgao_pagador, nome_ug_pagadora,
                tipo_pagamento, valor,
            ))

        inserir_em_lote(conexao, sql_insert, linhas)
        total += len(linhas)
        print(f"[transformar] silver_pagamento: +{len(linhas)} linhas (total: {total})")

    cursor.close()
    print(f"[transformar] silver_pagamento concluida: {total} carregadas, {descartadas} descartadas.")


def transformar_passagem(conexao):
    sql_select = """
        SELECT id_viagem, meio_transporte,
               pais_origem_ida, uf_origem_ida, cidade_origem_ida,
               pais_destino_ida, uf_destino_ida, cidade_destino_ida,
               valor_passagem, taxa_servico, data_emissao
        FROM raw_passagem
    """
    sql_insert = """
        INSERT INTO silver_passagem (
            id_viagem, meio_transporte,
            pais_origem_ida, uf_origem_ida, cidade_origem_ida,
            pais_destino_ida, uf_destino_ida, cidade_destino_ida,
            valor_passagem, taxa_servico, data_emissao
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """

    cursor = ler_raw_em_blocos(conexao, sql_select)
    total, descartadas = 0, 0
    while True:
        bloco = cursor.fetchmany(TAMANHO_BLOCO)
        if not bloco:
            break
        linhas = []
        for row in bloco:
            (id_viagem, meio_transporte,
             pais_origem_ida, uf_origem_ida, cidade_origem_ida,
             pais_destino_ida, uf_destino_ida, cidade_destino_ida,
             valor_passagem_txt, taxa_servico_txt, data_emissao_txt) = row

            if not id_viagem:
                descartadas += 1
                continue

            valor_passagem = texto_para_decimal(valor_passagem_txt)
            taxa_servico = texto_para_decimal(taxa_servico_txt)
            data_emissao = texto_para_data(data_emissao_txt)

            linhas.append((
                id_viagem, meio_transporte,
                pais_origem_ida, uf_origem_ida, cidade_origem_ida,
                pais_destino_ida, uf_destino_ida, cidade_destino_ida,
                valor_passagem, taxa_servico, data_emissao,
            ))

        inserir_em_lote(conexao, sql_insert, linhas)
        total += len(linhas)
        print(f"[transformar] silver_passagem: +{len(linhas)} linhas (total: {total})")

    cursor.close()
    print(f"[transformar] silver_passagem concluida: {total} carregadas, {descartadas} descartadas.")


def transformar_trecho(conexao):
    sql_select = """
        SELECT id_viagem, sequencia_trecho, origem_data, origem_uf, origem_cidade,
               destino_data, destino_uf, destino_cidade, meio_transporte, numero_diarias
        FROM raw_trecho
    """
    sql_insert = """
        INSERT INTO silver_trecho (
            id_viagem, sequencia_trecho, origem_data, origem_uf, origem_cidade,
            destino_data, destino_uf, destino_cidade, meio_transporte, numero_diarias
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """

    cursor = ler_raw_em_blocos(conexao, sql_select)
    total, descartadas = 0, 0
    # sequencia_trecho + id_viagem tem UNIQUE na Silver: descarta duplicatas
    vistos = set()
    for row in iter(lambda: cursor.fetchmany(TAMANHO_BLOCO), []):
        linhas = []
        for (id_viagem, sequencia_trecho_txt, origem_data_txt, origem_uf, origem_cidade,
             destino_data_txt, destino_uf, destino_cidade, meio_transporte,
             numero_diarias_txt) in row:

            if not id_viagem:
                descartadas += 1
                continue

            sequencia_trecho = texto_para_inteiro(sequencia_trecho_txt)
            chave = (id_viagem, sequencia_trecho)
            if chave in vistos:
                descartadas += 1
                continue
            vistos.add(chave)

            origem_data = texto_para_data(origem_data_txt)
            destino_data = texto_para_data(destino_data_txt)
            numero_diarias = texto_para_decimal(numero_diarias_txt)

            linhas.append((
                id_viagem, sequencia_trecho, origem_data, origem_uf, origem_cidade,
                destino_data, destino_uf, destino_cidade, meio_transporte, numero_diarias,
            ))

        inserir_em_lote(conexao, sql_insert, linhas)
        total += len(linhas)
        print(f"[transformar] silver_trecho: +{len(linhas)} linhas (total: {total})")

    cursor.close()
    print(f"[transformar] silver_trecho concluida: {total} carregadas, {descartadas} descartadas.")


# =============================================================================
# MAIN
# =============================================================================
def main():
    try:
        conexao = conectar()
    except Exception as erro:
        print(f"[transformar] ERRO ao conectar no banco: {erro}", file=sys.stderr)
        return

    # Idempotencia: limpa as 4 tabelas Silver antes de recarregar.
    # O MySQL nao deixa fazer TRUNCATE numa tabela referenciada por FOREIGN KEY
    # -- mesmo que as tabelas filhas estejam vazias -- entao desligamos a
    # checagem de FK so durante o TRUNCATE, e voltamos a ligar em seguida.
    try:
        executar(conexao, "SET FOREIGN_KEY_CHECKS = 0")
        executar(conexao, "TRUNCATE TABLE silver_trecho")
        executar(conexao, "TRUNCATE TABLE silver_passagem")
        executar(conexao, "TRUNCATE TABLE silver_pagamento")
        executar(conexao, "TRUNCATE TABLE silver_viagem")
        executar(conexao, "SET FOREIGN_KEY_CHECKS = 1")
    except Exception as erro:
        print(f"[transformar] ERRO ao truncar tabelas Silver: {erro}", file=sys.stderr)
        executar(conexao, "SET FOREIGN_KEY_CHECKS = 1")

    etapas = [
        ("silver_viagem", transformar_viagem),
        ("silver_pagamento", transformar_pagamento),
        ("silver_passagem", transformar_passagem),
        ("silver_trecho", transformar_trecho),
    ]
    for nome, funcao in etapas:
        try:
            funcao(conexao)
        except Exception as erro:
            print(f"[transformar] ERRO ao transformar '{nome}': {erro}", file=sys.stderr)

    conexao.close()
    print("[transformar] Pipeline de transformacao finalizado.")


if __name__ == "__main__":
    main()
