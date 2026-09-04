r"""
Módulo de Scan de Documentos do Servidor
Escaneia \\SERVIDOR\clientes B&A e salva metadados na tabela documentos_pendentes.
O upload só acontece quando o usuário aprova na UI.

Duas funções principais:
- executar_scan(): escaneia servidor → salva metadados (sem upload)
- processar_aprovados(): pega docs com status='aprovado' → faz upload → atualiza cliente
"""
import os
import re
import json
import time
import logging
import unicodedata
from pathlib import Path
from datetime import datetime, date

logger = logging.getLogger("agente-varredura")

PASTA_SERVIDOR = Path(r"\\SERVIDOR\clientes B&A")

PADROES_DOC = {
    "Alvará":       [r"alvara", r"alvará"],
    "CND":          [r"\bcnd\b", r"certid[aã]o\s*negativa", r"\bnegativa\b"],
    "CNPJ":         [r"\bcnpj\b", r"cart[aã]o\s*cnpj"],
    "Contrato":     [r"contrato\s*social", r"altera[cç][aã]o\s*contrat"],
    "Licença":      [r"licen[cç]a", r"licenciamento"],
    "Inscrição":    [r"inscri[cç][aã]o", r"\bie\b", r"\bim\b"],
    "Certificado":  [r"certificado\s*digital", r"e-?cnpj", r"e-?cpf", r"\ba[13]\b.*certif",
                      r"certif.*\ba[13]\b"],
    "Nota Fiscal":  [r"nota\s*fiscal", r"\bnfe?\b", r"\bnfse\b", r"\bdanfe\b"],
    "Balancete":    [r"balancete", r"balanc.*verifica"],
    "Balanço":      [r"balan[cç]o", r"\bbp\b.*patrimon", r"patrimon.*\bbp\b"],
    "Relatório":    [r"relat[oó]rio\s*cont[aá]b", r"\bdre\b", r"demonstra[cç][aã]o",
                      r"demonstrativo"],
}

EXTENSOES_ACEITAS = {".pdf", ".jpg", ".jpeg", ".png", ".doc", ".docx",
                     ".xls", ".xlsx", ".pfx", ".p12", ".crt", ".cer", ".pem"}

STOP_WORDS = {"ltda", "me", "mei", "eireli", "epp", "sa", "s/a", "ss",
              "de", "do", "da", "dos", "das", "e", "em", "-", "s.a", "s.a."}


def _normalizar(texto):
    nfkd = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower().strip()


def _limpar_nome(texto):
    palavras = _normalizar(texto).replace("-", " ").replace(".", " ").split()
    return " ".join(p for p in palavras if p not in STOP_WORDS).strip()


def _match_score(nome_pasta, razao_social):
    a = _limpar_nome(nome_pasta)
    b = _limpar_nome(razao_social)
    if not a or not b:
        return 0
    if len(a.split()) < 2 and len(b.split()) < 2 and a != b:
        return 0
    if a == b:
        return 100
    if a in b or b in a:
        menor = min(len(a), len(b))
        maior = max(len(a), len(b))
        palavras_menor = len(min(a, b, key=len).split())
        if menor / maior >= 0.7 and palavras_menor >= 2:
            return 95
        return 0
    palavras_a = a.split()
    palavras_b = b.split()
    comuns = set(palavras_a) & set(palavras_b)
    if comuns == set(palavras_a) and comuns == set(palavras_b):
        return 90
    return 0


def classificar_arquivo(nome_arquivo):
    nome_lower = _normalizar(nome_arquivo)
    for tipo, padroes in PADROES_DOC.items():
        for padrao in padroes:
            if re.search(padrao, nome_lower):
                return tipo
    return None


def extrair_subtipo_cnd(nome_arquivo):
    nome = _normalizar(nome_arquivo)
    if "federal" in nome or "rfb" in nome or "uniao" in nome:
        return "Federal"
    if "estadual" in nome or "sefaz" in nome or "icms" in nome:
        return "Estadual"
    if "municipal" in nome or "iss" in nome or "prefeitura" in nome:
        return "Municipal"
    if "fgts" in nome:
        return "FGTS"
    if "trabalh" in nome or "tst" in nome:
        return "Trabalhista"
    if "falencia" in nome:
        return "Falência"
    return None


def extrair_subtipo_alvara(nome_arquivo):
    nome = _normalizar(nome_arquivo)
    if "sanit" in nome:
        return "Sanitário"
    if "bombeiro" in nome:
        return "Bombeiros"
    if "localiz" in nome or "funciona" in nome:
        return "Localização"
    if "provisor" in nome:
        return "Provisório"
    return None


def extrair_subtipo_certificado(nome_arquivo):
    nome = _normalizar(nome_arquivo)
    if "a1" in nome:
        return "A1"
    if "a3" in nome:
        return "A3"
    if "e-cnpj" in nome or "ecnpj" in nome:
        return "A1"
    if "e-cpf" in nome or "ecpf" in nome:
        return "A1"
    return None


def extrair_subtipo_nota_fiscal(nome_arquivo):
    nome = _normalizar(nome_arquivo)
    if "nfse" in nome:
        return "NFSe"
    if "danfe" in nome:
        return "DANFE"
    if "nfe" in nome:
        return "NFe"
    if "nf" in nome:
        return "NF"
    return None


def extrair_subtipo_relatorio(nome_arquivo):
    nome = _normalizar(nome_arquivo)
    if "dre" in nome:
        return "DRE"
    if "demonstra" in nome:
        return "Demonstração"
    return None


def extrair_periodo_do_nome(nome_arquivo):
    """Extrai competência/período do nome (MM/AAAA ou MM-AAAA)."""
    nome = Path(nome_arquivo).stem
    m = re.search(r"(\d{2})[-/](\d{4})", nome)
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    m = re.search(r"(jan|fev|mar|abr|mai|jun|jul|ago|set|out|nov|dez)\w*[-_\s]*(\d{4})", _normalizar(nome))
    if m:
        meses = {"jan": "01", "fev": "02", "mar": "03", "abr": "04", "mai": "05", "jun": "06",
                 "jul": "07", "ago": "08", "set": "09", "out": "10", "nov": "11", "dez": "12"}
        mes_num = meses.get(m.group(1)[:3], "00")
        return f"{mes_num}/{m.group(2)}"
    return None


def extrair_validade_do_nome(nome_arquivo):
    nome = Path(nome_arquivo).stem
    m = re.search(r"(\d{2})-(\d{2})-(\d{2,4})", nome)
    if m:
        dia, mes, ano = m.group(1), m.group(2), m.group(3)
        if len(ano) == 2:
            ano = "20" + ano
        try:
            return date(int(ano), int(mes), int(dia)).isoformat()
        except ValueError:
            pass
    m = re.search(r"(\d{2})/(\d{2})/(\d{2,4})", nome)
    if m:
        dia, mes, ano = m.group(1), m.group(2), m.group(3)
        if len(ano) == 2:
            ano = "20" + ano
        try:
            return date(int(ano), int(mes), int(dia)).isoformat()
        except ValueError:
            pass
    return None


def _sanitizar_nome_storage(texto):
    nome = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii")
    nome = re.sub(r"[^\w_.-]", "_", nome)
    nome = re.sub(r"_+", "_", nome)
    return nome.strip("_")


def _montar_nome_display(tipo, nome_arquivo):
    arq = Path(nome_arquivo)
    if tipo == "CND":
        subtipo = extrair_subtipo_cnd(nome_arquivo)
        validade = extrair_validade_do_nome(nome_arquivo)
        if subtipo:
            nome = f"CND {subtipo}"
            if validade:
                nome += f" {validade}"
            return nome
        return f"CND - {arq.stem}"
    elif tipo == "Alvará":
        subtipo = extrair_subtipo_alvara(nome_arquivo)
        if subtipo:
            return f"Alvará {subtipo}"
        return f"Alvará - {arq.stem}"
    elif tipo == "Contrato":
        return arq.stem
    elif tipo == "CNPJ":
        return "Cartão CNPJ"
    elif tipo == "Inscrição":
        if re.search(r"\bie\b", _normalizar(nome_arquivo)):
            return "Inscrição Estadual"
        elif re.search(r"\bim\b", _normalizar(nome_arquivo)):
            return "Inscrição Municipal"
        return f"Inscrição - {arq.stem}"
    elif tipo == "Licença":
        return f"Licença - {arq.stem}"
    elif tipo == "Certificado":
        subtipo = extrair_subtipo_certificado(nome_arquivo) or ""
        validade = extrair_validade_do_nome(nome_arquivo)
        nome = f"Certificado {subtipo}".strip()
        if validade:
            nome += f" - Venc. {validade}"
        return nome
    elif tipo == "Nota Fiscal":
        subtipo = extrair_subtipo_nota_fiscal(nome_arquivo) or ""
        periodo = extrair_periodo_do_nome(nome_arquivo) or ""
        nome = f"Nota Fiscal {subtipo}".strip()
        if periodo:
            nome += f" {periodo}"
        return nome if nome != "Nota Fiscal" else f"Nota Fiscal - {arq.stem}"
    elif tipo == "Balancete":
        periodo = extrair_periodo_do_nome(nome_arquivo)
        if periodo:
            return f"Balancete {periodo}"
        return f"Balancete - {arq.stem}"
    elif tipo == "Balanço":
        periodo = extrair_periodo_do_nome(nome_arquivo)
        if periodo:
            return f"Balanço {periodo}"
        return f"Balanço - {arq.stem}"
    elif tipo == "Relatório":
        subtipo = extrair_subtipo_relatorio(nome_arquivo) or ""
        periodo = extrair_periodo_do_nome(nome_arquivo) or ""
        nome = f"Relatório {subtipo}".strip()
        if periodo:
            nome += f" {periodo}"
        return nome if nome != "Relatório" else f"Relatório - {arq.stem}"
    return arq.stem


def _content_type(ext):
    tipos = {
        ".pdf": "application/pdf",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".doc": "application/msword",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".xls": "application/vnd.ms-excel",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".pfx": "application/x-pkcs12",
        ".p12": "application/x-pkcs12",
        ".crt": "application/x-x509-ca-cert",
        ".cer": "application/x-x509-ca-cert",
        ".pem": "application/x-pem-file",
    }
    return tipos.get(ext.lower(), "application/octet-stream")


# ============================================================
# PARTE 1: SCAN (só metadados, sem upload)
# ============================================================

def executar_scan(supabase_client, config):
    """
    Escaneia pastas do servidor e salva metadados na tabela documentos_pendentes.
    NÃO faz upload. Apenas registra o que encontrou.
    """
    stats = {"clientes": 0, "matches": 0, "novos": 0, "ja_pendentes": 0, "erros": 0}

    logger.info("[SCAN-DOCS] Iniciando scan de documentos do servidor...")

    try:
        resp = supabase_client.table("clientes").select("*").execute()
        clientes = resp.data or []
    except Exception as e:
        logger.error(f"[SCAN-DOCS] Erro ao buscar clientes: {e}")
        return stats

    stats["clientes"] = len(clientes)
    logger.info(f"[SCAN-DOCS] {len(clientes)} clientes no banco")
    if not clientes:
        return stats

    if not PASTA_SERVIDOR.exists():
        logger.warning(f"[SCAN-DOCS] Servidor inacessível: {PASTA_SERVIDOR}")
        return stats

    try:
        pastas = [p for p in PASTA_SERVIDOR.iterdir() if p.is_dir()]
    except (PermissionError, OSError) as e:
        logger.error(f"[SCAN-DOCS] Erro ao listar pastas: {e}")
        return stats

    logger.info(f"[SCAN-DOCS] {len(pastas)} pastas no servidor")

    matches = []
    for pasta in pastas:
        melhor_score = 0
        melhor_cliente = None
        for c in clientes:
            razao = c.get("razaoSocial", "") or ""
            fantasia = c.get("nomeFantasia", "") or ""
            score = max(
                _match_score(pasta.name, razao),
                _match_score(pasta.name, fantasia) if fantasia else 0,
            )
            if score > melhor_score:
                melhor_score = score
                melhor_cliente = c
        if melhor_score >= 90:
            matches.append((pasta, melhor_cliente, melhor_score))

    stats["matches"] = len(matches)
    logger.info(f"[SCAN-DOCS] {len(matches)} pastas com match")

    # Buscar caminhos já conhecidos (pendente/aprovado/importado)
    try:
        resp_pendentes = supabase_client.table("documentos_pendentes") \
            .select("caminho_servidor,status") \
            .in_("status", ["pendente", "aprovado", "importado"]) \
            .execute()
        caminhos_conhecidos = {r["caminho_servidor"] for r in (resp_pendentes.data or [])}
    except Exception as e:
        logger.error(f"[SCAN-DOCS] Erro ao buscar pendentes: {e}")
        caminhos_conhecidos = set()

    for pasta, cliente, score in matches:
        try:
            _catalogar_pasta(supabase_client, pasta, cliente, caminhos_conhecidos, stats)
        except Exception as e:
            logger.error(f"[SCAN-DOCS] Erro ao catalogar {pasta.name}: {e}")
            stats["erros"] += 1

    logger.info(
        f"[SCAN-DOCS] Scan concluído: {stats['novos']} novos, "
        f"{stats['ja_pendentes']} já conhecidos"
    )
    return stats


def _catalogar_pasta(supabase_client, pasta, cliente, caminhos_conhecidos, stats):
    """Escaneia pasta e insere metadados dos docs encontrados."""
    cliente_id = cliente.get("id")
    razao = cliente.get("razaoSocial", "?")

    docs_existentes = cliente.get("documentos") or []
    if isinstance(docs_existentes, str):
        try:
            docs_existentes = json.loads(docs_existentes)
        except (json.JSONDecodeError, TypeError):
            docs_existentes = []
    nomes_existentes = {_normalizar(d.get("nome", "")) for d in docs_existentes if d}

    try:
        arquivos = list(pasta.rglob("*"))
    except (PermissionError, OSError):
        return

    novos = []

    for arq in arquivos:
        try:
            if not arq.is_file():
                continue
        except (PermissionError, OSError):
            continue

        if arq.suffix.lower() not in EXTENSOES_ACEITAS:
            continue

        tipo = classificar_arquivo(arq.name)
        if not tipo:
            continue

        caminho = str(arq)

        if caminho in caminhos_conhecidos:
            stats["ja_pendentes"] += 1
            continue

        nome_display = _montar_nome_display(tipo, arq.name)

        if _normalizar(nome_display) in nomes_existentes:
            stats["ja_pendentes"] += 1
            continue

        validade = extrair_validade_do_nome(arq.name)
        subtipo = None
        if tipo == "CND":
            subtipo = extrair_subtipo_cnd(arq.name)
        elif tipo == "Alvará":
            subtipo = extrair_subtipo_alvara(arq.name)
        elif tipo == "Certificado":
            subtipo = extrair_subtipo_certificado(arq.name)
        elif tipo == "Nota Fiscal":
            subtipo = extrair_subtipo_nota_fiscal(arq.name)
        elif tipo == "Relatório":
            subtipo = extrair_subtipo_relatorio(arq.name)

        try:
            tamanho = arq.stat().st_size
        except (PermissionError, OSError):
            tamanho = None

        novos.append({
            "cliente_id": cliente_id,
            "razao_social": razao,
            "nome_arquivo": arq.name,
            "nome_display": nome_display,
            "tipo": tipo,
            "subtipo": subtipo,
            "validade": validade,
            "caminho_servidor": caminho,
            "tamanho_bytes": tamanho,
            "status": "pendente",
        })
        caminhos_conhecidos.add(caminho)

    if novos:
        try:
            supabase_client.table("documentos_pendentes").insert(novos).execute()
            stats["novos"] += len(novos)
            logger.info(f"[SCAN-DOCS] [{razao}] {len(novos)} documentos catalogados")
        except Exception as e:
            logger.error(f"[SCAN-DOCS] [{razao}] Erro ao inserir: {e}")
            stats["erros"] += 1


# ============================================================
# PARTE 2: PROCESSAR APROVADOS (upload + atualizar cliente)
# ============================================================

def processar_aprovados(supabase_client, config):
    """
    Busca docs com status='aprovado', faz upload, adiciona em documentos do cliente.
    """
    bucket = config.get("bucket_storage", "documentos-clientes")
    prefix = config.get("storage_path_prefix", "pdfs")

    try:
        resp = supabase_client.table("documentos_pendentes") \
            .select("*").eq("status", "aprovado").execute()
        aprovados = resp.data or []
    except Exception as e:
        logger.error(f"[SCAN-DOCS] Erro ao buscar aprovados: {e}")
        return

    if not aprovados:
        return

    logger.info(f"[SCAN-DOCS] {len(aprovados)} documento(s) aprovado(s) para importar")

    por_cliente = {}
    for doc in aprovados:
        cid = doc["cliente_id"]
        por_cliente.setdefault(cid, []).append(doc)

    for cliente_id, docs in por_cliente.items():
        try:
            _importar_docs_cliente(supabase_client, cliente_id, docs, bucket, prefix)
        except Exception as e:
            logger.error(f"[SCAN-DOCS] Erro importando cliente {cliente_id}: {e}")
            for doc in docs:
                try:
                    supabase_client.table("documentos_pendentes") \
                        .update({"status": "erro"}).eq("id", doc["id"]).execute()
                except Exception:
                    pass


def _importar_docs_cliente(supabase_client, cliente_id, docs_aprovados, bucket, prefix):
    """Faz upload e atualiza arrays documentos/certificados de um cliente."""
    try:
        resp = supabase_client.table("clientes") \
            .select("id,razaoSocial,documentos,certificados") \
            .eq("id", cliente_id).execute()
        if not resp.data:
            logger.warning(f"[SCAN-DOCS] Cliente {cliente_id} não encontrado")
            return
        cliente = resp.data[0]
    except Exception as e:
        logger.error(f"[SCAN-DOCS] Erro ao buscar cliente {cliente_id}: {e}")
        return

    razao = cliente.get("razaoSocial", "?")
    docs_existentes = cliente.get("documentos") or []
    if isinstance(docs_existentes, str):
        try:
            docs_existentes = json.loads(docs_existentes)
        except (json.JSONDecodeError, TypeError):
            docs_existentes = []

    certs_existentes = cliente.get("certificados") or []
    if isinstance(certs_existentes, str):
        try:
            certs_existentes = json.loads(certs_existentes)
        except (json.JSONDecodeError, TypeError):
            certs_existentes = []

    docs_atualizados = list(docs_existentes)
    certs_atualizados = list(certs_existentes)
    houve_mudanca_docs = False
    houve_mudanca_certs = False

    for doc_pendente in docs_aprovados:
        caminho = doc_pendente["caminho_servidor"]
        arq = Path(caminho)

        if not arq.exists():
            logger.warning(f"[SCAN-DOCS] Arquivo não encontrado: {caminho}")
            supabase_client.table("documentos_pendentes") \
                .update({"status": "erro"}).eq("id", doc_pendente["id"]).execute()
            continue

        tipo = doc_pendente.get("tipo", "")
        is_cert = tipo == "Certificado"
        storage_prefix = "certificados" if is_cert else prefix

        nome_storage = _sanitizar_nome_storage(
            f"{_sanitizar_nome_storage(razao)}_{doc_pendente['tipo']}_{int(time.time())}_{_sanitizar_nome_storage(arq.stem)}{arq.suffix.lower()}"
        )
        storage_path = f"{storage_prefix}/{nome_storage}"

        try:
            with open(arq, "rb") as f:
                supabase_client.storage.from_(bucket).upload(
                    storage_path, f.read(),
                    file_options={"cache-control": "3600", "upsert": "true",
                                  "content-type": _content_type(arq.suffix)},
                )
            url = supabase_client.storage.from_(bucket).get_public_url(storage_path)
            if not url:
                raise ValueError("URL não obtida")
        except Exception as e:
            logger.warning(f"[SCAN-DOCS] Erro upload {arq.name}: {e}")
            supabase_client.table("documentos_pendentes") \
                .update({"status": "erro"}).eq("id", doc_pendente["id"]).execute()
            continue

        if is_cert:
            subtipo_cert = extrair_subtipo_certificado(arq.name) or "A1"
            validade = doc_pendente.get("validade")
            cert_final = {
                "nome": doc_pendente["nome_display"],
                "tipo": subtipo_cert,
                "vencimento": validade,
                "arquivo": {
                    "nome": arq.name,
                    "url": url,
                    "tamanho": doc_pendente.get("tamanho_bytes") or 0,
                    "uploadEm": datetime.now().isoformat(),
                },
            }
            logger.info(f"[SCAN-DOCS] [{razao}] Importando certificado: \"{doc_pendente['nome_display']}\"")
            certs_atualizados.append(cert_final)
            houve_mudanca_certs = True
        else:
            doc_final = {
                "nome": doc_pendente["nome_display"],
                "tipo": doc_pendente["tipo"],
                "validade": doc_pendente.get("validade"),
                "url": url,
                "uploadEm": datetime.now().isoformat(),
                "origem": "agente-scan",
            }

            idx = _encontrar_substituicao(doc_pendente, docs_atualizados)
            if idx >= 0:
                antigo = docs_atualizados[idx]
                logger.info(f"[SCAN-DOCS] [{razao}] Substituindo: \"{antigo.get('nome', '?')}\" → \"{doc_pendente['nome_display']}\"")
                docs_atualizados[idx] = doc_final
            else:
                logger.info(f"[SCAN-DOCS] [{razao}] Importando: \"{doc_pendente['nome_display']}\"")
                docs_atualizados.append(doc_final)

            houve_mudanca_docs = True

        supabase_client.table("documentos_pendentes").update({
            "status": "importado",
            "url": url,
            "importado_em": datetime.now().isoformat(),
        }).eq("id", doc_pendente["id"]).execute()

    update_data = {}
    if houve_mudanca_docs:
        update_data["documentos"] = docs_atualizados
    if houve_mudanca_certs:
        update_data["certificados"] = certs_atualizados

    if update_data:
        try:
            supabase_client.table("clientes").update(update_data) \
                .eq("id", cliente_id).execute()
            partes = []
            if houve_mudanca_docs:
                partes.append(f"{len(docs_atualizados)} docs")
            if houve_mudanca_certs:
                partes.append(f"{len(certs_atualizados)} certs")
            logger.info(f"[SCAN-DOCS] [{razao}] Banco atualizado ({', '.join(partes)})")
        except Exception as e:
            logger.error(f"[SCAN-DOCS] [{razao}] Erro ao salvar: {e}")


def _encontrar_substituicao(doc_pendente, docs_existentes):
    """
    CND/CNPJ/Inscrição: substitui mesmo subtipo.
    Alvará: substitui mesmo subtipo.
    Contrato/Nota Fiscal: NUNCA substitui (acumula).
    Balancete/Balanço/Relatório: substitui se mesmo período.
    Certificado: tratado separadamente (vai para array certificados).
    """
    tipo = doc_pendente.get("tipo", "")
    subtipo = doc_pendente.get("subtipo")

    if tipo in ("Contrato", "Nota Fiscal", "Certificado"):
        return -1

    if tipo == "CND" and subtipo:
        for i, ex in enumerate(docs_existentes):
            if ex.get("tipo") != "CND":
                continue
            if extrair_subtipo_cnd(ex.get("nome", "")) == subtipo:
                return i

    if tipo == "CNPJ":
        for i, ex in enumerate(docs_existentes):
            if ex.get("tipo") == "CNPJ":
                return i

    if tipo == "Inscrição":
        for i, ex in enumerate(docs_existentes):
            if ex.get("tipo") == "Inscrição":
                return i

    if tipo == "Alvará" and subtipo:
        for i, ex in enumerate(docs_existentes):
            if ex.get("tipo") != "Alvará":
                continue
            if extrair_subtipo_alvara(ex.get("nome", "")) == subtipo:
                return i

    if tipo in ("Balancete", "Balanço", "Relatório"):
        periodo_novo = extrair_periodo_do_nome(doc_pendente.get("nome_arquivo", ""))
        if periodo_novo:
            for i, ex in enumerate(docs_existentes):
                if ex.get("tipo") != tipo:
                    continue
                nome_ex = ex.get("nome", "")
                if periodo_novo in nome_ex:
                    return i

    return -1
