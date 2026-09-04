"""
Agente de Varredura - Monitor de Guias Fiscais
Monitora uma pasta, lê PDFs de guias (DAS, DARF, GPS, etc.),
identifica o cliente pelo CNPJ e faz a baixa automática no sistema.
"""

import os
import sys
import json
import re
import time
import shutil
import logging
import threading
import unicodedata
import atexit
from datetime import datetime, date, timezone
from pathlib import Path

from supabase import create_client
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver
from watchdog.events import FileSystemEventHandler
import pymupdf

# Módulo de scan de documentos do servidor
from scan_documentos import executar_scan as _executar_scan_documentos
from scan_documentos import processar_aprovados as _processar_aprovados

# Servidor API local (explorador de arquivos)
from servidor_api import iniciar_servidor as _iniciar_servidor_api, _resolver_pasta_cliente
from scan_documentos import PASTA_SERVIDOR, _normalizar

# OCR para PDFs imagem (DESTDA, etc.) — Windows OCR via winocr (0.65s vs 43s RapidOCR)
try:
    import winocr
    from PIL import Image
    _OCR_DISPONIVEL = True
except ImportError:
    _OCR_DISPONIVEL = False

# ============================================================
# IDENTIFICAÇÃO DA MÁQUINA
# ============================================================

import socket
import getpass

def _gerar_machine_id() -> str:
    """Gera ID unico da maquina: HOSTNAME\\username"""
    return f"{socket.gethostname()}\\{getpass.getuser()}"

MACHINE_ID = _gerar_machine_id()

# ============================================================
# VERSAO
#
# Precisa ser bumpada a cada release publicada no GitHub. E ela que o
# auto-update compara com a tag da release mais recente.
# ============================================================
VERSAO = "1.1.3"
REPO_API_LATEST = "https://api.github.com/repos/lukas913/rnx-agente-varredura/releases/latest"
NOME_ASSET = "AgenteVarredura.exe"

# ============================================================
# CONFIGURAÇÃO
# ============================================================

# ============================================================
# PASTA DE DADOS — a mesma em qualquer maquina, sempre.
#
# O .exe pode acabar em Downloads, na Area de Trabalho ou em Program Files.
# Se os dados morassem ao lado dele, cada usuario teria um comportamento
# diferente — e em Program Files nem ha permissao de escrita. Por isso
# config, logs e pastas de trabalho vivem SEMPRE em %LOCALAPPDATA%\RNX-Agente.
#
# Rodando como script (desenvolvimento) continua na pasta do projeto, para
# nao misturar teste com a instalacao real.
# ============================================================
if getattr(sys, 'frozen', False):
    BASE_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "RNX-Agente"
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    # Migracao das instalacoes antigas, que guardavam o config ao lado do .exe
    _cfg_antigo = Path(sys.executable).parent / "config.json"
    _cfg_novo = BASE_DIR / "config.json"
    if _cfg_antigo.exists() and not _cfg_novo.exists():
        try:
            shutil.copy2(_cfg_antigo, _cfg_novo)
        except Exception:
            pass
else:
    BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"

# ============================================================
# PRIMEIRO RUN: Login + Config de pasta
# ============================================================

def _setup_primeiro_run():
    """Janela de setup para primeiro uso: login no Supabase + pasta monitorada."""
    import tkinter as tk
    from tkinter import filedialog, messagebox

    # Carrega config existente ou cria default
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            cfg = json.load(f)
    else:
        cfg = {
            "supabase_url": "https://nessxxdsvsxekdwtpbrm.supabase.co",
            "supabase_key": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im5lc3N4eGRzdnN4ZWtkd3RwYnJtIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NTA3NzA4NTgsImV4cCI6MjA2NjM0Njg1OH0.38Ypo6kU2RKxDDbo3ATYSj9SrjjzIvtOULCSvHShPXA",
            "pasta_monitorada": "./_entrada",
            "pasta_processados": "./_processados",
            "pasta_erro": "./_erro",
            "pasta_logs": "./_logs",
            "intervalo_verificacao": 2,
            "bucket_storage": "documentos-clientes",
            "storage_path_prefix": "pdfs"
        }

    resultado = {"ok": False}

    root = tk.Tk()
    root.title("Agente de Varredura - Configuracao")
    root.geometry("480x340")
    root.resizable(False, False)
    root.configure(bg="#2c3e50")

    # Centralizar
    root.update_idletasks()
    x = (root.winfo_screenwidth() // 2) - 240
    y = (root.winfo_screenheight() // 2) - 170
    root.geometry(f"+{x}+{y}")

    tk.Label(root, text="Agente de Varredura", font=("Segoe UI", 16, "bold"),
             bg="#2c3e50", fg="white").pack(pady=(15, 5))
    tk.Label(root, text="Configure seu acesso ao sistema", font=("Segoe UI", 9),
             bg="#2c3e50", fg="#bdc3c7").pack(pady=(0, 15))

    frame = tk.Frame(root, bg="#34495e", padx=20, pady=15)
    frame.pack(fill="x", padx=20)

    tk.Label(frame, text="Email:", bg="#34495e", fg="white", font=("Segoe UI", 10)).grid(row=0, column=0, sticky="w", pady=3)
    email_var = tk.StringVar()
    tk.Entry(frame, textvariable=email_var, width=35, font=("Segoe UI", 10)).grid(row=0, column=1, pady=3, padx=(8, 0))

    tk.Label(frame, text="Senha:", bg="#34495e", fg="white", font=("Segoe UI", 10)).grid(row=1, column=0, sticky="w", pady=3)
    senha_var = tk.StringVar()
    tk.Entry(frame, textvariable=senha_var, width=35, font=("Segoe UI", 10), show="*").grid(row=1, column=1, pady=3, padx=(8, 0))

    tk.Label(frame, text="Pasta:", bg="#34495e", fg="white", font=("Segoe UI", 10)).grid(row=2, column=0, sticky="w", pady=3)
    pasta_var = tk.StringVar(value=cfg.get("pasta_monitorada", ""))
    pasta_entry = tk.Entry(frame, textvariable=pasta_var, width=28, font=("Segoe UI", 10))
    pasta_entry.grid(row=2, column=1, pady=3, padx=(8, 0), sticky="w")

    def escolher_pasta():
        p = filedialog.askdirectory(title="Pasta com os PDFs de guias")
        if p:
            pasta_var.set(p)

    tk.Button(frame, text="...", command=escolher_pasta, width=3).grid(row=2, column=1, sticky="e", pady=3)

    status_label = tk.Label(root, text="", bg="#2c3e50", fg="#e74c3c", font=("Segoe UI", 9))
    status_label.pack(pady=(8, 0))

    def fazer_login():
        email = email_var.get().strip()
        senha = senha_var.get().strip()
        pasta = pasta_var.get().strip()

        if not email or not senha:
            status_label.config(text="Preencha email e senha")
            return
        if not pasta:
            status_label.config(text="Selecione a pasta monitorada")
            return

        status_label.config(text="Autenticando...", fg="#f39c12")
        root.update()

        try:
            sb = create_client(cfg["supabase_url"], cfg["supabase_key"])
            auth_resp = sb.auth.sign_in_with_password({"email": email, "password": senha})
            user = auth_resp.user
            if not user:
                status_label.config(text="Login falhou - verifique credenciais", fg="#e74c3c")
                return

            # Verifica se usuario tem perfil no sistema
            perfil = sb.table("usuarios").select("id, nome, funcao, permissoes").eq("id", user.id).limit(1).execute()
            if not perfil.data:
                status_label.config(text="Usuario sem perfil no sistema", fg="#e74c3c")
                return

            dados_perfil = perfil.data[0]
            nome_usuario = dados_perfil.get("nome", email)
            role = dados_perfil.get("funcao", "")

            # Verifica se tem permissao para usar o agente (gerente sempre pode)
            if role != "gerente":
                permissoes = dados_perfil.get("permissoes") or {}
                funcionalidades = permissoes.get("funcionalidades") or {}
                if not funcionalidades.get("agente_varredura", False):
                    status_label.config(text="Agente nao liberado para seu usuario", fg="#e74c3c")
                    return

            # Salvar config
            cfg["user_id"] = user.id
            cfg["user_email"] = email
            cfg["user_nome"] = nome_usuario
            # A senha NAO e guardada. O refresh_token abaixo e rotativo e se
            # renova a cada uso, entao com o agente rodando ele nunca expira.
            # Guardar a senha em texto puro no config.json de cada usuario seria
            # espalhar credencial por todas as maquinas do escritorio.
            cfg.pop("user_password", None)
            cfg["refresh_token"] = auth_resp.session.refresh_token if auth_resp.session else None
            cfg["pasta_monitorada"] = pasta
            cfg["pasta_processados"] = str(Path(pasta) / "_processados")
            cfg["pasta_erro"] = str(Path(pasta) / "_erro")
            cfg["machine_id"] = MACHINE_ID

            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=4, ensure_ascii=False)

            resultado["ok"] = True
            root.destroy()

        except Exception as e:
            status_label.config(text=f"Erro: {str(e)[:60]}", fg="#e74c3c")

    tk.Button(root, text="Entrar e Configurar", command=fazer_login,
              bg="#27ae60", fg="white", font=("Segoe UI", 11, "bold"),
              relief="flat", padx=20, pady=6, cursor="hand2").pack(pady=15)

    # Registrar no Startup do Windows
    auto_start_var = tk.BooleanVar(value=True)
    tk.Checkbutton(root, text="Iniciar com o Windows", variable=auto_start_var,
                   bg="#2c3e50", fg="white", selectcolor="#34495e",
                   activebackground="#2c3e50", activeforeground="white",
                   font=("Segoe UI", 9)).pack()

    root.mainloop()

    # Se login OK e auto-start marcado, registra no Startup
    if resultado["ok"] and auto_start_var.get():
        _registrar_startup()

    return resultado["ok"]


def _registrar_startup():
    """Registra o agente no Startup do Windows (atalho .lnk)."""
    try:
        import winreg
        startup_dir = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
        vbs_path = BASE_DIR / "iniciar-agente.vbs"

        # Se tiver o .vbs, cria atalho para ele (janela oculta)
        if vbs_path.exists():
            target = str(vbs_path)
        else:
            # Se for .exe (PyInstaller), cria atalho direto
            target = str(Path(sys.executable).resolve())

        # Usar PowerShell para criar o .lnk
        lnk_path = startup_dir / "Agente Varredura.lnk"
        ps_cmd = f'''
$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut("{lnk_path}")
$lnk.TargetPath = "{target}"
$lnk.WorkingDirectory = "{BASE_DIR}"
$lnk.Save()
'''
        os.system(f'powershell -NoProfile -Command "{ps_cmd.strip()}"')
    except Exception:
        pass


def _reautenticar():
    """Janela mínima que pede só a senha para gerar refresh_token.
    Usada quando config já tem user_id/email mas falta o token."""
    import tkinter as tk

    with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
        cfg = json.load(f)

    email = cfg.get("user_email", "")
    if not email:
        return False

    resultado = {"ok": False}

    root = tk.Tk()
    root.title("Agente - Reautenticação")
    root.geometry("400x200")
    root.resizable(False, False)
    root.configure(bg="#2c3e50")
    root.update_idletasks()
    x = (root.winfo_screenwidth() // 2) - 200
    y = (root.winfo_screenheight() // 2) - 100
    root.geometry(f"+{x}+{y}")

    tk.Label(root, text="Sessão expirada", font=("Segoe UI", 14, "bold"),
             bg="#2c3e50", fg="white").pack(pady=(15, 3))
    tk.Label(root, text=f"Digite a senha de {email}", font=("Segoe UI", 9),
             bg="#2c3e50", fg="#bdc3c7").pack(pady=(0, 12))

    frame = tk.Frame(root, bg="#34495e", padx=20, pady=12)
    frame.pack(fill="x", padx=20)
    tk.Label(frame, text="Senha:", bg="#34495e", fg="white", font=("Segoe UI", 10)).pack(side="left")
    senha_var = tk.StringVar()
    tk.Entry(frame, textvariable=senha_var, width=28, font=("Segoe UI", 10), show="*").pack(side="left", padx=(8, 0))

    status_label = tk.Label(root, text="", bg="#2c3e50", fg="#e74c3c", font=("Segoe UI", 9))
    status_label.pack(pady=(6, 0))

    def fazer_reauth():
        senha = senha_var.get().strip()
        if not senha:
            status_label.config(text="Digite a senha")
            return
        status_label.config(text="Autenticando...", fg="#f39c12")
        root.update()
        try:
            sb = create_client(cfg["supabase_url"], cfg["supabase_key"])
            auth_resp = sb.auth.sign_in_with_password({"email": email, "password": senha})
            if not auth_resp.user or not auth_resp.session:
                status_label.config(text="Senha incorreta", fg="#e74c3c")
                return
            cfg["refresh_token"] = auth_resp.session.refresh_token
            cfg.pop("user_password", None)   # nao guardamos mais a senha
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=4, ensure_ascii=False)
            resultado["ok"] = True
            root.destroy()
        except Exception as e:
            status_label.config(text=f"Erro: {str(e)[:50]}", fg="#e74c3c")

    tk.Button(root, text="Entrar", command=fazer_reauth,
              bg="#27ae60", fg="white", font=("Segoe UI", 10, "bold"),
              relief="flat", padx=16, pady=4, cursor="hand2").pack(pady=10)

    root.mainloop()
    return resultado["ok"]


# Carrega config ou pede setup
if CONFIG_PATH.exists():
    with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
        CONFIG = json.load(f)
else:
    CONFIG = {}

# Se não tem user_id configurado, pede login na primeira vez
USER_ID = CONFIG.get("user_id")
if not USER_ID:
    if not _setup_primeiro_run():
        sys.exit(0)  # Usuário fechou sem configurar
    with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
        CONFIG = json.load(f)
    USER_ID = CONFIG["user_id"]

# Se tem user_id mas não tem refresh_token, pede só a senha
if not CONFIG.get("refresh_token"):
    if not _reautenticar():
        print("Reautenticação cancelada. O agente não conseguirá enviar para triagem.")
    else:
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            CONFIG = json.load(f)

PASTA_MONITORADA = (BASE_DIR / CONFIG["pasta_monitorada"]).resolve()
PASTA_PROCESSADOS = (BASE_DIR / CONFIG["pasta_processados"]).resolve()
PASTA_ERRO = (BASE_DIR / CONFIG["pasta_erro"]).resolve()
PASTA_LOGS = (BASE_DIR / CONFIG["pasta_logs"]).resolve()

# Garante que pastas existam
for pasta in [PASTA_MONITORADA, PASTA_PROCESSADOS, PASTA_ERRO, PASTA_LOGS]:
    pasta.mkdir(parents=True, exist_ok=True)

# Logger
log_file = PASTA_LOGS / f"agente_{date.today().isoformat()}.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(log_file, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("agente-varredura")

# ============================================================
# AUTO-ATUALIZACAO (GitHub Releases)
#
# Todo mundo baixa o .exe do mesmo lugar: a release mais recente do
# repositorio. No boot o agente pergunta ao GitHub qual a versao publicada;
# se for maior que a dele, baixa, confere o tamanho e se substitui usando um
# .cmd que espera este processo encerrar.
#
# Sem isso, quem instalou uma vez fica preso naquela versao para sempre — foi
# exatamente o que aconteceu com a build de abril/2026, que rodou quatro meses
# atras do codigo.
# ============================================================

def _versao_tupla(v):
    """'v1.10.2' -> (1, 10, 2). Comparar como tupla de numeros evita o erro
    classico de o texto '1.9' parecer maior que '1.10'."""
    nums = re.findall(r"\d+", str(v or ""))
    return tuple(int(n) for n in nums[:3]) if nums else (0,)


def verificar_atualizacao():
    """Baixa e instala a release mais nova. True = este processo vai encerrar."""
    if not getattr(sys, "frozen", False):
        logger.info(f"[UPDATE] Rodando como script (v{VERSAO}) — auto-update desligado")
        return False

    import urllib.request
    cabecalho = {"User-Agent": f"RNX-AgenteVarredura/{VERSAO}"}

    try:
        req = urllib.request.Request(
            REPO_API_LATEST,
            headers=dict(cabecalho, Accept="application/vnd.github+json"))
        with urllib.request.urlopen(req, timeout=20) as r:
            info = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        # Sem internet ou GitHub fora: segue com a versao atual, nao e fatal.
        logger.warning(f"[UPDATE] Nao consegui consultar o GitHub: {e}")
        return False

    tag = info.get("tag_name") or ""
    if _versao_tupla(tag) <= _versao_tupla(VERSAO):
        logger.info(f"[UPDATE] Ja esta na versao mais recente (v{VERSAO})")
        return False

    asset = next((a for a in info.get("assets", []) if a.get("name") == NOME_ASSET), None)
    if not asset:
        logger.warning(f"[UPDATE] Release {tag} nao publicou o arquivo {NOME_ASSET}")
        return False

    logger.info(f"[UPDATE] Versao nova: {tag} (atual v{VERSAO}) — baixando...")
    exe_atual = Path(sys.executable)
    novo = exe_atual.parent / f"{exe_atual.stem}.novo.exe"
    try:
        req = urllib.request.Request(asset["browser_download_url"], headers=cabecalho)
        with urllib.request.urlopen(req, timeout=600) as r, open(novo, "wb") as f:
            shutil.copyfileobj(r, f)
    except Exception as e:
        logger.error(f"[UPDATE] Falha ao baixar: {e}")
        try: novo.unlink(missing_ok=True)
        except Exception: pass
        return False

    # Conferencia simples de integridade: baixou tudo mesmo?
    esperado = int(asset.get("size") or 0)
    if esperado and novo.stat().st_size != esperado:
        logger.error(f"[UPDATE] Download incompleto ({novo.stat().st_size} de {esperado} bytes) — abortado")
        try: novo.unlink(missing_ok=True)
        except Exception: pass
        return False

    # Um .exe em execucao nao pode sobrescrever a si mesmo. O .cmd abaixo espera
    # este PID sumir da lista de processos, troca o arquivo e reabre o agente.
    pid = os.getpid()
    bat = exe_atual.parent / "_atualizar.cmd"
    # O .cmd deixa rastro proprio: ele roda depois que este processo morreu,
    # entao nao ha como registrar o que aconteceu no log do agente. Sem isso a
    # troca falhava em silencio e so dava para diagnosticar por eliminacao.
    log_troca = BASE_DIR / "_logs" / "atualizacao.log"
    # newline="" e obrigatorio: sem ele o Python traduz cada \n para \r\n e o
    # arquivo sai com \r\r\n. O cmd nao aceita — o rotulo vira ":esperar\r", o
    # "goto esperar" nao encontra o destino e o script morre antes de trocar o
    # executavel. Foi exatamente o que aconteceu no primeiro teste: o download
    # completou, o .cmd foi criado, e nada foi substituido.
    # A pergunta certa nao e "o processo morreu?" e sim "o arquivo ja pode ser
    # substituido?". Tentar o move em laco responde as duas de uma vez: enquanto
    # o .exe estiver em uso o Windows recusa, e no instante em que liberar, passa.
    #
    # Tres armadilhas do cmd que este formato evita, todas encontradas testando:
    #   - `timeout` exige console e falha na hora quando o processo nasce sem um,
    #     transformando a espera em laco infinito. Usamos `ping` para pausar.
    #   - `goto` dentro de um bloco entre parenteses tem comportamento traicoeiro.
    #     Aqui nao ha nenhum bloco.
    #   - depender de `tasklist | find "<pid>"` quebra conforme o idioma do
    #     Windows, que muda a mensagem de "nenhuma tarefa encontrada".
    #
    # Limite de 60 tentativas (~2 min). Se estourar, fica registrado no log em
    # vez de tentar para sempre.
    bat.write_text(
        "@echo off\r\n"
        f'echo [%date% %time%] iniciado (agente pid {pid}) >> "{log_troca}"\r\n'
        "set /a tentativas=0\r\n"
        ":tentar\r\n"
        "set /a tentativas+=1\r\n"
        f'move /y "{novo}" "{exe_atual}" >nul 2>&1\r\n'
        f'if not exist "{novo}" goto pronto\r\n'
        "if %tentativas% geq 60 goto desistiu\r\n"
        "ping -n 2 127.0.0.1 >nul\r\n"
        "goto tentar\r\n"
        ":pronto\r\n"
        f'echo [%date% %time%] executavel substituido apos %tentativas% tentativa(s) >> "{log_troca}"\r\n'
        f'start "" "{exe_atual}"\r\n'
        f'echo [%date% %time%] agente reiniciado >> "{log_troca}"\r\n'
        'del "%~f0"\r\n'
        "exit /b\r\n"
        ":desistiu\r\n"
        f'echo [%date% %time%] DESISTI: o executavel seguiu bloqueado apos %tentativas% tentativas >> "{log_troca}"\r\n'
        "exit /b\r\n",
        encoding="utf-8", newline="")

    # Somente DETACHED_PROCESS. A versao anterior combinava com CREATE_NO_WINDOW
    # e o Windows nao aceita os dois juntos — sao mutuamente exclusivos, e o
    # processo simplesmente nao subia. O .cmd ficava no disco intacto e o
    # executavel nunca era trocado, sem erro nenhum aparecer.
    # DETACHED_PROCESS ja garante que nao aparece janela: o processo nasce sem
    # console. E precisa ser destacado mesmo, para sobreviver a saida deste.
    try:
        import subprocess
        DETACHED_PROCESS = 0x00000008
        proc = subprocess.Popen(["cmd", "/c", str(bat)],
                                creationflags=DETACHED_PROCESS, close_fds=True)
        logger.info(f"[UPDATE] Script de troca disparado (pid {proc.pid})")
    except Exception as e:
        logger.error(f"[UPDATE] Falha ao disparar a troca: {e}")
        return False

    logger.info(f"[UPDATE] Reiniciando na versao {tag}...")
    return True


logger.info(f"Agente de Varredura v{VERSAO} — maquina {MACHINE_ID}")
logger.info(f"Pasta de dados: {BASE_DIR}")
if verificar_atualizacao():
    sys.exit(0)

# Supabase
supabase = create_client(CONFIG["supabase_url"], CONFIG["supabase_key"])

# Restaurar sessão autenticada (sem isso, RLS bloqueia INSERT na triagem)
_refresh_token = CONFIG.get("refresh_token")
_sessao_ok = False

def _aplicar_sessao(session_resp):
    """Propaga access_token para o client postgrest (headers explícitos).
    Também corrige USER_ID caso o config esteja desatualizado."""
    global supabase, USER_ID
    try:
        token = session_resp.session.access_token
        supabase.postgrest.auth(token)
        real_uid = str(session_resp.user.id)
        if USER_ID != real_uid:
            logger.warning(f"   USER_ID config ({USER_ID}) != auth.uid ({real_uid}) — corrigindo!")
            USER_ID = real_uid
            CONFIG["user_id"] = real_uid
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(CONFIG, f, indent=4, ensure_ascii=False)
        logger.info(f"   access_token propagado para postgrest (uid={real_uid})")
        return True
    except Exception as e:
        logger.warning(f"   Falha ao propagar access_token: {e}")
        return False

if _refresh_token:
    try:
        _session = supabase.auth.refresh_session(_refresh_token)
        if _session and _session.session:
            # Atualiza refresh_token no config caso tenha sido rotacionado
            new_rt = _session.session.refresh_token
            if new_rt and new_rt != _refresh_token:
                CONFIG["refresh_token"] = new_rt
                with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                    json.dump(CONFIG, f, indent=4, ensure_ascii=False)
            _aplicar_sessao(_session)
            _sessao_ok = True
            logger.info("Sessão autenticada restaurada com sucesso")
    except Exception as _auth_err:
        logger.warning(f"Refresh token expirado ({_auth_err}), pedindo reautenticação...")
        # Token expirado — pede senha de novo
        if _reautenticar():
            with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
                CONFIG = json.load(f)
            try:
                _session2 = supabase.auth.refresh_session(CONFIG["refresh_token"])
                if _session2 and _session2.session:
                    _aplicar_sessao(_session2)
                    _sessao_ok = True
                    logger.info("Sessão restaurada após reautenticação")
            except Exception:
                pass

# Limpeza das instalacoes antigas: com a sessao ja autenticada pelo
# refresh_token, a senha em texto puro nao serve mais para nada e sai do disco.
# Feito so DEPOIS de autenticar, para nao remover o unico recurso disponivel
# caso o refresh_token esteja mesmo perdido.
if _sessao_ok and CONFIG.pop("user_password", None) is not None:
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(CONFIG, f, indent=4, ensure_ascii=False)
        logger.info("Senha em texto puro removida do config.json — nao e mais necessaria")
    except Exception as e:
        logger.warning(f"Nao consegui limpar a senha do config.json: {e}")

if not _sessao_ok:
    logger.warning("⚠ Rodando SEM sessão autenticada — triagem pode falhar (RLS)")

# ============================================================
# REFRESH AUTOMÁTICO DE TOKEN (evita JWT expirado durante execução)
# ============================================================
_ultimo_refresh = time.time() if _sessao_ok else 0
_REFRESH_INTERVALO = 45 * 60  # Renova a cada 45 min (JWT expira em 60 min)

def _tentar_refresh_token():
    """Renova o access_token usando refresh_token.
    Prioridade: 1) sessão ativa do gotrue (pode ter feito auto-refresh)
                2) CONFIG salvo no config.json
    Se ambos falharem, tenta sign_in_with_password como último recurso."""
    global _sessao_ok, _ultimo_refresh, CONFIG
    try:
        # 1. Pegar o refresh_token mais recente da sessão ativa do gotrue
        #    (o client pode ter feito auto-refresh e consumido o token antigo)
        rt = None
        try:
            current = supabase.auth.get_session()
            if current and current.refresh_token:
                rt = current.refresh_token
        except Exception:
            pass
        # 2. Fallback: usar o CONFIG salvo
        if not rt:
            rt = CONFIG.get("refresh_token")
        if not rt:
            return _tentar_reauth_senha()
        resp = supabase.auth.refresh_session(rt)
        if resp and resp.session:
            new_rt = resp.session.refresh_token
            if new_rt and new_rt != CONFIG.get("refresh_token"):
                CONFIG["refresh_token"] = new_rt
                try:
                    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                        json.dump(CONFIG, f, indent=4, ensure_ascii=False)
                except Exception:
                    pass  # OneDrive lock — token fica em memória
            _aplicar_sessao(resp)
            _sessao_ok = True
            _ultimo_refresh = time.time()
            logger.info("Token renovado automaticamente")
            return True
    except Exception as e:
        logger.warning(f"Falha ao renovar token: {e}")
    # 3. Último recurso: re-autenticar com email/senha
    logger.info("Tentando re-autenticacao com email/senha...")
    return _tentar_reauth_senha()


def _tentar_reauth_senha():
    """Ultimo recurso: login por email/senha, se houver senha guardada.

    Desde a v1.1.1 o agente NAO guarda mais a senha, entao este caminho so
    existe para instalacoes antigas que ainda tenham o campo. O caminho normal
    e o refresh_token, que rotaciona a cada uso e nao expira com o agente
    rodando. Se ele morrer de vez, o usuario reabre o agente e loga de novo."""
    global _sessao_ok, _ultimo_refresh, CONFIG
    email = CONFIG.get("user_email")
    senha = CONFIG.get("user_password")
    if not email or not senha:
        logger.warning("Sessao expirada e sem senha guardada — abra o agente e "
                       "faca login novamente para reativar a sincronizacao")
        _sessao_ok = False
        return False
    try:
        resp = supabase.auth.sign_in_with_password({"email": email, "password": senha})
        if resp and resp.session:
            new_rt = resp.session.refresh_token
            if new_rt:
                CONFIG["refresh_token"] = new_rt
                try:
                    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                        json.dump(CONFIG, f, indent=4, ensure_ascii=False)
                except Exception:
                    pass
            _aplicar_sessao(resp)
            _sessao_ok = True
            _ultimo_refresh = time.time()
            logger.info("Re-autenticado com email/senha automaticamente")
            return True
    except Exception as e:
        logger.warning(f"Falha ao re-autenticar com senha: {e}")
    _sessao_ok = False
    return False

# ============================================================
# CONFIGURAÇÃO DINÂMICA (puxa pastas do Supabase)
# ============================================================

def carregar_config_supabase():
    """Puxa configuração de pastas do Supabase (definida no modal do sistema).
    Tenta buscar config específica do user_id, senão usa a config geral (legado)."""
    try:
        # Primeiro: tenta config do usuário específico
        if USER_ID:
            resp = supabase.table("agente_varredura_config").select("*").eq("user_id", USER_ID).limit(1).execute()
            if resp.data:
                cfg = resp.data[0]
                pasta_mon = cfg.get("pasta_monitorada", "").strip()
                if pasta_mon:
                    pasta_proc = cfg.get("pasta_processados", "").strip()
                    pasta_err = cfg.get("pasta_erro", "").strip()
                    return {
                        "pasta_monitorada": Path(pasta_mon).resolve(),
                        "pasta_processados": Path(pasta_proc).resolve() if pasta_proc else Path(pasta_mon).resolve() / "_processados",
                        "pasta_erro": Path(pasta_err).resolve() if pasta_err else Path(pasta_mon).resolve() / "_erro",
                    }
        # Fallback: config geral (sem user_id — legado, seu PC atual)
        resp = supabase.table("agente_varredura_config").select("*").is_("user_id", "null").limit(1).execute()
        if not resp.data:
            resp = supabase.table("agente_varredura_config").select("*").limit(1).execute()
        if resp.data:
            cfg = resp.data[0]
            pasta_mon = cfg.get("pasta_monitorada", "").strip()
            pasta_proc = cfg.get("pasta_processados", "").strip()
            pasta_err = cfg.get("pasta_erro", "").strip()
            if pasta_mon:
                return {
                    "pasta_monitorada": Path(pasta_mon).resolve(),
                    "pasta_processados": Path(pasta_proc).resolve() if pasta_proc else Path(pasta_mon).resolve() / "_processados",
                    "pasta_erro": Path(pasta_err).resolve() if pasta_err else Path(pasta_mon).resolve() / "_erro",
                }
    except Exception as e:
        logger.warning(f"Nao foi possivel carregar config do Supabase: {e}")
    return None

# Tenta carregar do Supabase primeiro, senao usa config.json local
_config_remota = carregar_config_supabase()
if _config_remota:
    PASTA_MONITORADA = _config_remota["pasta_monitorada"]
    PASTA_PROCESSADOS = _config_remota["pasta_processados"]
    PASTA_ERRO = _config_remota["pasta_erro"]
    logger.info("Config de pastas carregada do Supabase")
else:
    logger.info("Usando config de pastas do config.json local")

# Garante que pastas existam
for pasta in [PASTA_MONITORADA, PASTA_PROCESSADOS, PASTA_ERRO, PASTA_LOGS]:
    pasta.mkdir(parents=True, exist_ok=True)

# ============================================================
# FILTRO DE CLIENTES POR USUÁRIO
# ============================================================

def limpar_cnpj(cnpj: str) -> str:
    """Remove formatação do CNPJ, mantendo só dígitos."""
    return re.sub(r"\D", "", cnpj)

_CNPJS_PERMITIDOS: set[str] | None = None  # None = sem filtro (gerente)

def _carregar_cnpjs_usuario():
    """Carrega CNPJs dos clientes vinculados a este usuário.
    Se gerente ou sem vínculos, retorna None (sem filtro)."""
    global _CNPJS_PERMITIDOS
    try:
        # Verifica se é gerente
        perfil = supabase.table("usuarios").select("funcao").eq("id", USER_ID).limit(1).execute()
        if perfil.data and perfil.data[0].get("funcao") == "gerente":
            _CNPJS_PERMITIDOS = None
            return

        # Busca vínculos usuario_clientes.
        #
        # A consulta antiga era .select("cnpj").eq("user_id", ...) e devolvia 400:
        # a tabela nao tem nenhuma dessas duas colunas. As colunas reais sao
        # usuario_id, cliente_id e setores (conferido no isolamento-dados.js do
        # RNX, que le a mesma tabela). O 400 caia no except abaixo, que zerava o
        # filtro — ou seja, o analista processava documento de TODO cliente.
        #
        # Sao duas etapas de proposito, usando so colunas confirmadas: depender
        # de relacionamento embutido (clientes(cnpj)) quebraria de novo caso a
        # foreign key nao esteja declarada no banco.
        vinc = supabase.table("usuario_clientes").select("cliente_id").eq("usuario_id", USER_ID).execute()
        ids = [r["cliente_id"] for r in (vinc.data or []) if r.get("cliente_id")]
        if not ids:
            # Sem vínculos definidos = sem filtro (mantém retrocompatibilidade)
            _CNPJS_PERMITIDOS = None
            return

        cli = supabase.table("clientes").select("cnpj").in_("id", ids).execute()
        cnpjs = {limpar_cnpj(r["cnpj"]) for r in (cli.data or []) if r.get("cnpj")}
        _CNPJS_PERMITIDOS = cnpjs or None
        logger.info(f"   {len(cnpjs)} cliente(s) vinculado(s) a este usuário")
    except Exception as e:
        # Antes esta falha era silenciosa e virava "sem filtro". Agora aparece.
        logger.warning(f"Nao consegui carregar os clientes vinculados ({e}) — seguindo SEM filtro de CNPJ")
        _CNPJS_PERMITIDOS = None

_carregar_cnpjs_usuario()

def _cnpj_permitido(cnpj: str) -> bool:
    """Verifica se este CNPJ está nos clientes permitidos para este usuário."""
    if _CNPJS_PERMITIDOS is None:
        return True  # Sem filtro = permite todos
    return limpar_cnpj(cnpj) in _CNPJS_PERMITIDOS


# ============================================================
# APRENDIZADO PELA TRIAGEM (Forma 1) — o agente aprende com as correções
# ============================================================

_APRENDIZADO: list[dict] = []

def carregar_aprendizado():
    """Carrega as 'aulas' que vieram das correções na triagem (tabela agente_aprendizado).
    Cada linha associa o texto de um documento ao tipo/descrição que um humano confirmou.
    (Nota: PostgREST corta em 1000 linhas — paginar se a tabela crescer muito.)"""
    global _APRENDIZADO
    try:
        resp = (
            supabase.table("agente_aprendizado")
            .select("descricao_evento, tipo_guia, cliente_id, cnpj, texto_amostra")
            .eq("ativo", True)
            .execute()
        )
        _APRENDIZADO = resp.data or []
        logger.info(f"   {len(_APRENDIZADO)} exemplo(s) de aprendizado carregado(s) da triagem")
    except Exception as e:
        logger.warning(f"Não foi possível carregar aprendizado: {e}")

carregar_aprendizado()

# Estatísticas da sessão
stats = {
    "processados": 0,
    "erros": 0,
    "inicio": datetime.now().isoformat(),
}

# Arquivos já processados (não reprocessar)
_arquivos_processados: set[str] = set()

# Regex para detectar/remover prefixos de data empilhados (2026-03-17_2026-03-17_...)
_PREFIXO_DATA_RE = re.compile(r'^(\d{4}-\d{2}-\d{2}_)+')

def _nome_base(nome: str) -> str:
    """Remove prefixos de data empilhados do nome do arquivo."""
    return _PREFIXO_DATA_RE.sub('', nome)


# ============================================================
# HEARTBEAT (status do agente no Supabase)
# ============================================================

def heartbeat_loop():
    """Envia ping a cada 30s para o Supabase indicando que o agente esta vivo.
    A cada 2 pings (60s), recarrega a config do Supabase e reinicia o observer se necessario.
    Tambem processa docs aprovados a cada ping (~30s) para importacao quase instantanea.
    Renova o token automaticamente a cada 45 min para evitar JWT expirado."""
    global PASTA_MONITORADA, PASTA_PROCESSADOS, PASTA_ERRO, _ultimo_refresh
    ciclo = 0
    while True:
        # Renova token proativamente a cada 45 min
        if time.time() - _ultimo_refresh > _REFRESH_INTERVALO:
            _tentar_refresh_token()

        try:
            payload = {
                "ultimo_ping": datetime.now(timezone.utc).isoformat(),
                "processados_hoje": stats["processados"],
                "erros_hoje": stats["erros"],
                "machine_id": MACHINE_ID,
                "user_id": USER_ID,
                "user_nome": CONFIG.get("user_nome", ""),
                "pasta_monitorada": str(PASTA_MONITORADA),
            }
            # Upsert por machine_id: cada máquina tem sua linha de status
            resp = supabase.table("agente_varredura_status").select("id").eq("machine_id", MACHINE_ID).limit(1).execute()
            if resp.data:
                supabase.table("agente_varredura_status").update(payload).eq("id", resp.data[0]["id"]).execute()
            else:
                supabase.table("agente_varredura_status").insert(payload).execute()
        except Exception as hb_err:
            # Se JWT expirou, tenta refresh imediato
            if "401" in str(hb_err) or "JWT expired" in str(hb_err) or "PGRST301" in str(hb_err):
                logger.warning("JWT expirado detectado no heartbeat — tentando refresh...")
                _tentar_refresh_token()
            else:
                logger.debug(f"Heartbeat falhou: {hb_err}")

        # A cada 60s, verifica se a config mudou e se há pedido de scan
        ciclo += 1
        if ciclo % 2 == 0:
            # Recarrega as aulas da triagem (correções novas entram sem reiniciar)
            try:
                carregar_aprendizado()
            except Exception:
                pass
            # Arquiva no servidor as guias resolvidas na triagem (lote pequeno por ciclo)
            try:
                _arquivar_triagem_resolvidos()
            except Exception:
                pass
            try:
                nova_config = carregar_config_supabase()
                if nova_config and nova_config["pasta_monitorada"] != PASTA_MONITORADA:
                    logger.info(f"Config alterada no Supabase. Nova pasta: {nova_config['pasta_monitorada']}")
                    PASTA_MONITORADA = nova_config["pasta_monitorada"]
                    PASTA_PROCESSADOS = nova_config["pasta_processados"]
                    PASTA_ERRO = nova_config["pasta_erro"]
                    for pasta in [PASTA_MONITORADA, PASTA_PROCESSADOS, PASTA_ERRO]:
                        pasta.mkdir(parents=True, exist_ok=True)
                    # Sinaliza para o main reiniciar o observer
                    stats["config_changed"] = True
            except Exception:
                pass

        time.sleep(30)

# ============================================================
# REGRAS DE IDENTIFICAÇÃO DE GUIAS
# ============================================================

REGRAS_GUIAS = [
    {
        "tipo": "DAS",
        "descricoes_match": ["DAS"],
        "tipo_guia_banco": "DAS - Simples Nacional",
        "textos_pdf": [
            "DOCUMENTO DE ARRECADAÇÃO DO SIMPLES NACIONAL",
            "DAS - SIMPLES NACIONAL",
            "PROGRAMA GERADOR DO DOCUMENTO",
            "PGDAS",
        ],
    },
    {
        "tipo": "DAS-MEI",
        "descricoes_match": ["DAS-MEI", "DAS MEI", "MEI"],
        "tipo_guia_banco": "DAS-MEI",
        "textos_pdf": [
            "DAS-MEI",
            "MICROEMPREENDEDOR INDIVIDUAL",
            "DASN-SIMEI",
        ],
    },
    # --- DCTFWEB e EFD devem vir ANTES de DEFIS (DEFIS tem textos genéricos) ---
    {
        "tipo": "DCTFWEB",
        "descricoes_match": ["DCTFWEB", "DCTF-WEB", "DCTF WEB", "DCTFWeb"],
        "tipo_guia_banco": "DCTFWeb",
        "textos_pdf": [
            "DCTFWEB",
            "DCTF-WEB",
            "DCTF WEB",
            "DECLARAÇÃO DE DÉBITOS E CRÉDITOS TRIBUTÁRIOS FEDERAIS",
            "DECLARACAO DE DEBITOS E CREDITOS TRIBUTARIOS FEDERAIS",
            "DÉBITOS E CRÉDITOS TRIBUTÁRIOS FEDERAIS PREVIDENCIÁRIOS",
            "DEBITOS E CREDITOS TRIBUTARIOS FEDERAIS PREVIDENCIARIOS",
        ],
    },
    {
        "tipo": "EFD-CONTRIBUICOES",
        "descricoes_match": ["EFD-CONTRIBUICOES", "EFD CONTRIBUICOES", "EFD - CONTRIBUICOES", "EFD-CONTRIBUIÇÕES", "EFD CONTRIBUIÇÕES", "EFD - CONTRIBUIÇÕES", "EFD CONTRIBUI", "EFD - CONTRIBUI"],
        "tipo_guia_banco": "EFD Contribuições",
        "textos_pdf": [
            "EFD-CONTRIBUIÇÕES",
            "EFD-CONTRIBUICOES",
            "EFD CONTRIBUIÇÕES",
            "EFD CONTRIBUICOES",
            "ESCRITURAÇÃO FISCAL DIGITAL DAS CONTRIBUIÇÕES",
            "ESCRITURACAO FISCAL DIGITAL DAS CONTRIBUICOES",
            "EFD-PIS/COFINS",
        ],
    },
    {
        "tipo": "EFD-REINF",
        "descricoes_match": ["EFD-REINF", "EFD REINF", "REINF"],
        "tipo_guia_banco": "EFD-Reinf",
        "textos_pdf": [
            "EFD-REINF",
            "ESCRITURAÇÃO FISCAL DIGITAL DE RETENÇÕES",
            "ESCRITURACAO FISCAL DIGITAL DE RETENCOES",
        ],
    },
    {
        "tipo": "EFD-ICMS/IPI",
        "descricoes_match": ["EFD ICMS", "EFD-ICMS", "SPED FISCAL", "EFD ICMS/IPI"],
        "tipo_guia_banco": "EFD ICMS/IPI",
        "textos_pdf": [
            "EFD-ICMS/IPI",
            "ESCRITURAÇÃO FISCAL DIGITAL - ICMS",
            "ESCRITURACAO FISCAL DIGITAL - ICMS",
            "SPED FISCAL",
        ],
    },
    {
        "tipo": "ECD",
        "descricoes_match": ["ECD", "SPED CONTÁBIL", "SPED CONTABIL"],
        "tipo_guia_banco": "ECD - SPED Contábil",
        "anual": True,
        "textos_pdf": [
            "ESCRITURAÇÃO CONTÁBIL DIGITAL",
            "ESCRITURACAO CONTABIL DIGITAL",
            "SPED CONTÁBIL",
            "SPED CONTABIL",
        ],
    },
    {
        "tipo": "ECF",
        "descricoes_match": ["ECF"],
        "tipo_guia_banco": "ECF",
        "anual": True,
        "textos_pdf": [
            "ESCRITURAÇÃO CONTÁBIL FISCAL",
            "ESCRITURACAO CONTABIL FISCAL",
        ],
    },
    {
        "tipo": "DCTF",
        "descricoes_match": ["DCTF"],
        "tipo_guia_banco": "DCTF",
        "textos_pdf": [
            "DECLARAÇÃO DE DÉBITOS E CRÉDITOS TRIBUTÁRIOS FEDERAIS",
            "DECLARACAO DE DEBITOS E CREDITOS TRIBUTARIOS FEDERAIS",
        ],
    },
    {
        "tipo": "DEFIS",
        "descricoes_match": ["DEFIS"],
        "tipo_guia_banco": "DEFIS - Simples Nacional",
        "anual": True,
        "textos_pdf": [
            "DECLARAÇÃO DE INFORMAÇÕES SOCIOECONÔMICAS",
            "DECLARACAO DE INFORMACOES SOCIOECONOMICAS",
            "DEFIS",
        ],
    },
    {
        "tipo": "DESTDA",
        "descricoes_match": ["DESTDA"],
        "tipo_guia_banco": "DeSTDA",
        "textos_pdf": [
            "DESTDA",
            "DECLARAÇÃO DE SUBSTITUIÇÃO TRIBUTÁRIA",
            "DECLARACAO DE SUBSTITUICAO TRIBUTARIA",
        ],
    },
    {
        "tipo": "DARF",
        "descricoes_match": ["DARF"],
        "tipo_guia_banco": "DARF",
        "textos_pdf": [
            "DOCUMENTO DE ARRECADAÇÃO DE RECEITAS FEDERAIS",
            "DARF",
            "CÓDIGO DA RECEITA",
        ],
    },
    {
        "tipo": "GPS",
        "descricoes_match": ["GPS", "INSS"],
        "tipo_guia_banco": "GPS",
        "textos_pdf": [
            "GUIA DA PREVIDÊNCIA SOCIAL",
            "GPS",
            "INSTITUTO NACIONAL DO SEGURO SOCIAL",
        ],
    },
    {
        "tipo": "GRF-FGTS",
        "descricoes_match": ["FGTS", "GRF-FGTS", "GRF"],
        "tipo_guia_banco": "GRF-FGTS",
        "textos_pdf": [
            "GUIA DE RECOLHIMENTO DO FGTS",
            "GRF",
            "FUNDO DE GARANTIA",
        ],
    },
    {
        "tipo": "DEISS",
        "descricoes_match": ["DEISS", "DME", "DES-IF", "DISS"],
        "tipo_guia_banco": "DEISS",
        "textos_pdf": [
            "DEISS",
            "PROTOCOLO DE ENTREGA DE DME",
            "PROTOCOLO ENTREGA DME",
            "DECLARAÇÃO MENSAL ELETRÔNICA",
            "DECLARACAO MENSAL ELETRONICA",
            "DES-IF",
            "RECIBO DE ENTREGA - DECLARAÇÃO MENSAL",
            "RECIBO DE ENTREGA - DECLARACAO MENSAL",
            "DECLARAÇÃO MENSAL DE SERVIÇO",
            "DECLARACAO MENSAL DE SERVICO",
        ],
    },
    {
        "tipo": "ISS",
        "descricoes_match": ["ISS", "ISSQN"],
        "tipo_guia_banco": "ISS",
        "textos_pdf": [
            "IMPOSTO SOBRE SERVIÇO",
            "ISSQN",
        ],
    },
    {
        "tipo": "ICMS",
        "descricoes_match": ["ICMS", "GNRE"],
        "tipo_guia_banco": "ICMS",
        "textos_pdf": [
            "ICMS",
            "DARE",
            "GNRE",
            "GUIA NACIONAL DE RECOLHIMENTO",
        ],
    },
    {
        "tipo": "RAIS",
        "descricoes_match": ["RAIS"],
        "tipo_guia_banco": "RAIS",
        "anual": True,
        "textos_pdf": [
            "RELAÇÃO ANUAL DE INFORMAÇÕES SOCIAIS",
            "RELACAO ANUAL DE INFORMACOES SOCIAIS",
            "RAIS",
        ],
    },
    {
        "tipo": "DIRF",
        "descricoes_match": ["DIRF"],
        "tipo_guia_banco": "DIRF",
        "anual": True,
        "textos_pdf": [
            "DECLARAÇÃO DO IMPOSTO SOBRE A RENDA RETIDO NA FONTE",
            "DECLARACAO DO IMPOSTO SOBRE A RENDA RETIDO NA FONTE",
            "DIRF",
        ],
    },
    {
        "tipo": "DIMOB",
        "descricoes_match": ["DIMOB"],
        "tipo_guia_banco": "DIMOB",
        "anual": True,
        "textos_pdf": [
            "DECLARAÇÃO DE INFORMAÇÕES SOBRE ATIVIDADES IMOBILIÁRIAS",
            "DECLARACAO DE INFORMACOES SOBRE ATIVIDADES IMOBILIARIAS",
            "DIMOB",
        ],
    },
    {
        "tipo": "DMED",
        "descricoes_match": ["DMED"],
        "tipo_guia_banco": "DMED",
        "anual": True,
        "textos_pdf": [
            "DECLARAÇÃO DE SERVIÇOS MÉDICOS",
            "DECLARACAO DE SERVICOS MEDICOS",
            "DMED",
        ],
    },
]


# ============================================================
# MATCHING GENÉRICO POR SIMILARIDADE (zero manutenção)
# ============================================================

# Palavras irrelevantes para matching (preposições, artigos, etc.)
_STOP_WORDS_MATCH = {
    "de", "do", "da", "dos", "das", "e", "em", "o", "a", "os", "as",
    "no", "na", "nos", "nas", "ao", "aos", "para", "por", "com", "sem",
    "um", "uma", "que", "se", "ou", "pelo", "pela", "este", "esta",
    "esse", "essa", "aquele", "aquela", "seu", "sua", "é", "são",
    "foi", "ser", "ter", "como", "mais", "sobre", "já", "até",
    "recibo", "entrega", "protocolo", "comprovante",  # ruído comum em PDFs
}


def _tokenizar(texto: str) -> set[str]:
    """Extrai tokens significativos de um texto (upper, sem acentos, sem stop words).
    Gera tanto tokens compostos (EFD-CONTRIBUICOES) quanto partes separadas (EFD, CONTRIBUICOES)."""
    texto_norm = unicodedata.normalize("NFKD", texto.upper())
    texto_norm = "".join(c for c in texto_norm if not unicodedata.combining(c))
    # Captura tokens com hífen/barra (EFD-CONTRIBUICOES, PIS/COFINS, CTF/APP)
    tokens_raw = re.findall(r"[A-Z0-9][A-Z0-9/.-]*[A-Z0-9]|[A-Z0-9]{2,}", texto_norm)
    tokens = set()
    for t in tokens_raw:
        if t.lower() in _STOP_WORDS_MATCH or len(t) < 2:
            continue
        tokens.add(t)
        # Também adiciona partes separadas (EFD-CONTRIBUICOES → EFD, CONTRIBUICOES)
        if "-" in t or "/" in t:
            for parte in re.split(r"[-/]", t):
                if len(parte) >= 2 and parte.lower() not in _STOP_WORDS_MATCH:
                    tokens.add(parte)
    return tokens


def _score_similaridade(texto_pdf: str, descricao_evento: str) -> float:
    """Calcula score de similaridade entre texto do PDF e descrição do evento.
    Retorna 0.0 a 1.0. Quanto maior, mais provável o match."""
    tokens_pdf = _tokenizar(texto_pdf[:3000])  # Limita pra performance
    tokens_evento = _tokenizar(descricao_evento)

    if not tokens_evento:
        return 0.0

    # Quantos tokens do evento aparecem no PDF?
    hits = tokens_pdf & tokens_evento
    # Score = fração dos tokens do evento que aparecem no PDF
    score = len(hits) / len(tokens_evento)

    # Bonus: se a descrição INTEIRA (normalizada) aparece no texto do PDF, score máximo
    desc_norm = unicodedata.normalize("NFKD", descricao_evento.upper())
    desc_norm = "".join(c for c in desc_norm if not unicodedata.combining(c))
    pdf_norm = unicodedata.normalize("NFKD", texto_pdf[:5000].upper())
    pdf_norm = "".join(c for c in pdf_norm if not unicodedata.combining(c))
    if desc_norm in pdf_norm:
        score = 1.0

    return score


def _eh_guia_pagamento_por_texto(texto_pdf: str) -> bool:
    """Detecta se o PDF é uma guia de pagamento (precisa ser enviada ao cliente)
    usando padrões estruturais universais — sem lista fixa de tipos."""
    texto_upper = texto_pdf[:3000].upper()
    # Padrões estruturais que indicam guia de pagamento
    indicadores_guia = [
        r"DOCUMENTO DE ARRECADA[CÇ][AÃ]O",
        r"GUIA DE (?:RECOLHIMENTO|PAGAMENTO|ARRECADA)",
        r"TOTAL A (?:PAGAR|RECOLHER)",
        r"VALOR DO DOCUMENTO",
        r"C[OÓ]DIGO DE BARRAS",
        r"LINHA DIGIT[AÁ]VEL",
        r"VENCIMENTO.*R\$",
        r"AUTENTICAÇÃO BANCÁRIA",  # boletos
        r"BANCO.*AGÊNCIA.*CONTA",  # boletos bancários
    ]
    for padrao in indicadores_guia:
        if re.search(padrao, texto_upper):
            return True

    # Indicadores NEGATIVOS (recibo de entrega, certidão, etc.) — NÃO é guia
    indicadores_nao_guia = [
        r"RECIBO DE ENTREGA",
        r"PROTOCOLO DE (?:ENTREGA|TRANSMISS)",
        r"CERTID[AÃ]O",
        r"ALVAR[AÁ]",
        r"DECLARA[CÇ][AÃ]O.*ENTREGUE",
    ]
    for padrao in indicadores_nao_guia:
        if re.search(padrao, texto_upper):
            return False

    return False


def buscar_obrigacao_generica(cliente_id: int | None, cnpj: str | None,
                               nome_cliente: str | None, competencia: str | None,
                               texto_pdf: str) -> dict | None:
    """Busca genérica: traz TODAS as obrigações pendentes do cliente e ranqueia
    por similaridade com o texto do PDF. Não depende de lista fixa de tipos."""
    obrigacoes_pendentes = []

    # Estratégia 1: por cliente_id (FK, mais confiável)
    if cliente_id and competencia:
        resp = (
            supabase.table("eventos_rotina")
            .select("*")
            .eq("cliente_id", cliente_id)
            .eq("competencia", competencia)
            .eq("realizada", False)
            .execute()
        )
        obrigacoes_pendentes.extend(resp.data or [])

    # Estratégia 2: por CNPJ direto (caso cliente_id falhe)
    if not obrigacoes_pendentes and cnpj and competencia:
        cnpj_limpo = limpar_cnpj(cnpj)
        for cnpj_busca in [cnpj, cnpj_limpo]:
            resp = (
                supabase.table("eventos_rotina")
                .select("*")
                .eq("cnpj", cnpj_busca)
                .eq("competencia", competencia)
                .eq("realizada", False)
                .execute()
            )
            if resp.data:
                obrigacoes_pendentes.extend(resp.data)
                break

    # Estratégia 3: por nome do cliente (ILIKE, menos confiável)
    if not obrigacoes_pendentes and nome_cliente and competencia:
        resp = (
            supabase.table("eventos_rotina")
            .select("*")
            .ilike("cliente", f"%{nome_cliente}%")
            .eq("competencia", competencia)
            .eq("realizada", False)
            .execute()
        )
        obrigacoes_pendentes.extend(resp.data or [])

    if not obrigacoes_pendentes:
        return None

    # Ranqueia por similaridade
    melhor_score = 0.0
    melhor_match = None
    for obrigacao in obrigacoes_pendentes:
        desc = obrigacao.get("descricao", "")
        score = _score_similaridade(texto_pdf, desc)
        logger.debug(f"   Similaridade '{desc}': {score:.2f}")
        if score > melhor_score:
            melhor_score = score
            melhor_match = obrigacao

    # Threshold mínimo de 0.5 (pelo menos metade dos tokens da descrição no PDF)
    if melhor_match and melhor_score >= 0.5:
        logger.info(f"   Match genérico: '{melhor_match.get('descricao')}' (score={melhor_score:.2f})")
        return melhor_match

    # Se nenhum atingiu 0.5, loga os candidatos para debug
    if obrigacoes_pendentes:
        descs = [f"{o.get('descricao')} ({_score_similaridade(texto_pdf, o.get('descricao', '')):.2f})"
                 for o in obrigacoes_pendentes[:5]]
        logger.info(f"   {len(obrigacoes_pendentes)} obrigações pendentes, nenhuma com score >= 0.5: {descs}")

    return None


# ============================================================
# EXTRAÇÃO DE DADOS DO PDF
# ============================================================


def extrair_texto_pdf(caminho_pdf: Path) -> str:
    """Extrai todo o texto de um PDF. Se não tiver texto (imagem), usa Windows OCR."""
    texto = ""

    # pymupdf é rápido e não trava em PDFs imagem (retorna vazio direto)
    doc = pymupdf.open(str(caminho_pdf))
    for page in doc:
        t = page.get_text()
        if t and t.strip():
            texto += t + "\n"

    if texto.strip():
        doc.close()
        return texto

    # PDF imagem — tenta OCR
    if not _OCR_DISPONIVEL:
        doc.close()
        logger.warning(f"   PDF sem texto e OCR não disponível: {caminho_pdf.name}")
        return ""

    logger.info(f"   PDF imagem detectado, usando Windows OCR...")
    try:
        for page in doc:
            # Recorta só os 55% superiores (onde ficam CNPJ, empresa, competência)
            clip = pymupdf.Rect(0, 0, page.rect.width, page.rect.height * 0.55)
            pix = page.get_pixmap(dpi=150, clip=clip)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            result = winocr.recognize_pil_sync(img, lang="pt")
            if result and result["text"]:
                texto += result["text"] + "\n"
        if texto.strip():
            logger.info(f"   OCR extraiu {len(texto)} chars")
    except Exception as e:
        logger.warning(f"   Erro no OCR: {e}")
    finally:
        doc.close()

    return texto


def extrair_cnpj(texto: str) -> str | None:
    """Extrai CNPJ do texto (formato XX.XXX.XXX/XXXX-XX).
    Também lida com quirks do Windows OCR (troca 0→e, espaços extras)."""
    # Padrão direto: 32.013.570/0001-02
    padrao = r"\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}"
    match = re.search(padrao, texto)
    if match:
        return match.group(0)

    # OCR às vezes coloca CNPJ: numa linha e o número na próxima
    padrao_multiline = r"CNPJ[:\s]*\n?\s*(\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2})"
    match = re.search(padrao_multiline, texto, re.IGNORECASE)
    if match:
        return match.group(1)

    # Windows OCR quirk: troca 0 por "e"/"B"/"O", adiciona espaços
    # Ex: "27.562. e6B/eee1-54" → "27.562.060/0001-54"
    _OCR_DIGIT = r"[\deOoBbIl]"  # dígito ou substituto OCR
    padrao_ocr = (
        r"C\s*N\s*P\s*J\s*[:.\s]*\s*"
        rf"({_OCR_DIGIT}{{2}})\s*[.\s]+\s*"
        rf"({_OCR_DIGIT}{{3}})\s*[.\s]+\s*"
        rf"({_OCR_DIGIT}{{3}})\s*/\s*"
        rf"({_OCR_DIGIT}{{4}})\s*-\s*"
        rf"({_OCR_DIGIT}{{2}})"
    )
    match = re.search(padrao_ocr, texto, re.IGNORECASE)
    if match:
        partes = [match.group(i) for i in range(1, 6)]
        # Corrige substitutos OCR: B/b/e/O/o→0, I/l→1
        _OCR_MAP = str.maketrans({"e": "0", "O": "0", "o": "0", "B": "0", "b": "0", "I": "1", "l": "1"})
        cnpj_limpo = [p.translate(_OCR_MAP) for p in partes]
        return f"{cnpj_limpo[0]}.{cnpj_limpo[1]}.{cnpj_limpo[2]}/{cnpj_limpo[3]}-{cnpj_limpo[4]}"

    # CNPJ SEM formatação, como no Protocolo de Entrega DME (Ijuí):
    # "Cadastro: 732160 CNPJ: 48043105000154". Ancorado no rótulo "CNPJ" pra não pegar
    # cadastro/protocolo/código de barras. Aceita 13 dígitos (Ijuí às vezes solta o
    # zero inicial: "CNPJ: 9245555000178" = 09.245.555/0001-78) e completa com zfill.
    m = re.search(r"CNPJ[^\d]{0,6}(\d{13,14})(?!\d)", texto, re.IGNORECASE)
    if m:
        d = m.group(1).zfill(14)
        return f"{d[0:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:14]}"

    return None


def extrair_nome_empresa(texto: str) -> str | None:
    """Extrai razão social do texto do PDF quando CNPJ não foi encontrado.
    Procura padrões comuns: NOME + sufixo societário (LTDA, ME, S/A, EIRELI, SS, SLU, etc.)"""
    # Sufixos societários comuns — \b garante word boundary (não pega "ME" dentro de "MEDICOS")
    padrao = (
        r"([A-ZÀ-Ú][A-ZÀ-Ú0-9\s&\-\.]{3,80}?"
        r"\s+(?:LTDA|EIRELI|S/?A|S\.A\.|EPP|SLU|INDIVIDUAL|MICROEMPRESA|CIA|SS))\b"
        r"|"
        r"([A-ZÀ-Ú][A-ZÀ-Ú0-9\s&\-\.]{3,80}?\s+ME)\s*$"
    )
    matches = re.findall(padrao, texto.upper(), re.MULTILINE)
    if matches:
        # findall com 2 grupos retorna tuplas — pega o grupo não-vazio
        primeiro = matches[0]
        nome = primeiro[0] if primeiro[0] else primeiro[1]
        nome = re.sub(r"\s+", " ", nome).strip()
        # Ignora se for muito curto ou genérico
        if len(nome) > 8:
            return nome
    return None


def _corrigir_ano_ocr(ano_str: str) -> str:
    """Corrige quirks do Windows OCR no ano (B→0, e→0)."""
    return ano_str.replace("B", "0").replace("b", "0").replace("e", "0").replace("O", "0").replace("o", "0")


def extrair_competencia(texto: str) -> str | None:
    """
    Extrai competência do texto.
    Retorna no formato YYYY-MM para bater com o campo do banco.
    """
    # Mapa de meses por nome (OCR retorna "fev/2026", "mar/2026", etc.)
    _MESES = {
        "jan": "01", "fev": "02", "mar": "03", "abr": "04",
        "mai": "05", "jun": "06", "jul": "07", "ago": "08",
        "set": "09", "out": "10", "nov": "11", "dez": "12",
    }

    # Padrão: "Período de Apuração: 03/2026" ou "Competência: 03/2026"
    # Tolera OCR quirks no ano (B/e/O no lugar de 0)
    padrao_mes_ano = r"(?:compet[eê]ncia|per[ií]odo\s+de\s+apura[cç][aã]o|PA)[:\s]*(\d{2})[/\-]([0-9BbeOo]{4})"
    match = re.search(padrao_mes_ano, texto, re.IGNORECASE)
    if match:
        mes, ano = match.group(1), _corrigir_ano_ocr(match.group(2))
        return f"{ano}-{mes}"

    # Padrão OCR: "PERiODO: fev/2B26" ou "PERIODO: mar/2026" (case-insensitive)
    # Tolera quirks: ano com B/e/O no lugar de 0
    padrao_mes_nome = r"(?:compet[eê]ncia|per[iíI]od[o0]|PA)[:\s]*(jan|fev|mar|abr|mai|jun|jul|ago|set|out|nov|dez)[./\-]([0-9BbeOo]{4})"
    match = re.search(padrao_mes_nome, texto, re.IGNORECASE)
    if match:
        mes_nome = match.group(1).lower()[:3]
        ano = _corrigir_ano_ocr(match.group(2))
        mes_num = _MESES.get(mes_nome)
        if mes_num:
            return f"{ano}-{mes_num}"

    # Padrão: mês por extenso ou abreviado + /ano (sem rótulo obrigatório)
    # Ex: "Março/2026", "mar/2026", "Janeiro/2025"
    _MESES_EXTENSO = {
        "janeiro": "01", "fevereiro": "02", "março": "03", "marco": "03",
        "abril": "04", "maio": "05", "junho": "06", "julho": "07",
        "agosto": "08", "setembro": "09", "outubro": "10", "novembro": "11", "dezembro": "12",
    }
    padrao_mes_extenso = r"\b(janeiro|fevereiro|mar[cç]o|abril|maio|junho|julho|agosto|setembro|outubro|novembro|dezembro)\s*[/\-]\s*([0-9BbeOo]{4})\b"
    match = re.search(padrao_mes_extenso, texto, re.IGNORECASE)
    if match:
        mes_ext = match.group(1).lower()
        # Normaliza ç → c para lookup
        mes_ext_norm = unicodedata.normalize("NFKD", mes_ext)
        mes_ext_norm = "".join(c for c in mes_ext_norm if not unicodedata.combining(c))
        ano = _corrigir_ano_ocr(match.group(2))
        mes_num = _MESES_EXTENSO.get(mes_ext_norm) or _MESES.get(mes_ext_norm[:3])
        if mes_num:
            return f"{ano}-{mes_num}"

    # Padrão: "Ano e Mês de Referência: 2026/ 2" (DME)
    padrao_ano_mes_ref = r"(?:ano\s+e\s+m[eê]s\s+de\s+refer[eê]ncia|refer[eê]ncia)[:\s]*(\d{4})[/\s]+(\d{1,2})"
    match = re.search(padrao_ano_mes_ref, texto, re.IGNORECASE)
    if match:
        ano, mes = match.group(1), match.group(2).zfill(2)
        return f"{ano}-{mes}"

    # Padrão: "03/2026" genérico (mês/ano)
    # Usa negative lookbehind para não pegar DD/MM/YYYY (ex: "20/04/2026" → ignora "04/2026")
    padrao_generico = r"(?<!\d/)(\d{2})/(\d{4})\b"
    matches = re.findall(padrao_generico, texto)
    for mes, ano in matches:
        if 1 <= int(mes) <= 12 and 2020 <= int(ano) <= 2030:
            return f"{ano}-{mes}"

    return None


def extrair_ano_calendario(texto: str, nome_arquivo: str = "") -> str | None:
    """
    Extrai ano-calendário para obrigações anuais (DEFIS, RAPP, ECF, etc.).
    Retorna só o ano: "2025".
    """
    # Padrão 1: "Ano-calendário: 2025" ou "Ano Calendário 2025"
    padrao_ano_cal = r"ano[\s-]*calend[aá]rio[:\s]*(\d{4})"
    match = re.search(padrao_ano_cal, texto, re.IGNORECASE)
    if match:
        return match.group(1)

    # Padrão 2: "Exercício: 2025" ou "Exercicio 2025"
    padrao_exerc = r"exerc[ií]cio[:\s]*(\d{4})"
    match = re.search(padrao_exerc, texto, re.IGNORECASE)
    if match:
        return match.group(1)

    # Padrão 3: "Relatório: 2026/2025" → pega o MENOR ano (ano-calendário)
    #           Também pega "Relatorio 2025" simples
    padrao_relatorio = r"relat[oó]rio[:\s]*(\d{4})(?:/(\d{4}))?"
    match = re.search(padrao_relatorio, texto, re.IGNORECASE)
    if match:
        ano1 = match.group(1)
        ano2 = match.group(2)
        if ano2:
            return min(ano1, ano2)  # 2026/2025 → 2025 (ano-calendário)
        return ano1

    # Padrão 4: tenta extrair do nome do arquivo (ex: ReciboDEFIS-378172242025001.pdf → 2025)
    padrao_nome = r"(202[0-9]|203[0-9])"
    matches_nome = re.findall(padrao_nome, nome_arquivo)
    if matches_nome:
        return matches_nome[0]

    # Padrão 5: "Período: 01/01/2025 a 31/12/2025" → pega o ano
    padrao_periodo = r"per[ií]odo[:\s]*\d{2}/\d{2}/(\d{4})\s*a\s*\d{2}/\d{2}/\d{4}"
    match = re.search(padrao_periodo, texto, re.IGNORECASE)
    if match:
        return match.group(1)

    # Padrão 6: qualquer ano 4 dígitos razoável no texto
    padrao_generico = r"\b(202[0-9])\b"
    matches_gen = re.findall(padrao_generico, texto)
    if matches_gen:
        return matches_gen[0]

    return None


def extrair_vencimento(texto: str) -> str | None:
    """Extrai a data de vencimento do boleto/guia. Retorna ISO YYYY-MM-DD (pra bater com o banco)."""
    if not texto:
        return None
    padroes = [
        r"data\s+de\s+vencimento[:\s]*(\d{2})[/.\-](\d{2})[/.\-](\d{4})",
        r"vencimento[:\s]*(\d{2})[/.\-](\d{2})[/.\-](\d{4})",
        r"venc\.?[:\s]*(\d{2})[/.\-](\d{2})[/.\-](\d{4})",
    ]
    for pat in padroes:
        m = re.search(pat, texto, re.IGNORECASE)
        if m:
            d, mes, ano = m.group(1), m.group(2), m.group(3)
            try:
                if 1 <= int(d) <= 31 and 1 <= int(mes) <= 12 and 2020 <= int(ano) <= 2035:
                    return f"{ano}-{mes}-{d}"
            except ValueError:
                pass
    return None


def extrair_valor(texto: str) -> float | None:
    """Extrai o valor principal da guia."""
    # "Valor Total: R$ 1.234,56" ou "VALOR DO DOCUMENTO 1.234,56"
    padrao = r"(?:valor\s+(?:total|do\s+documento|principal|a\s+recolher))[:\s]*R?\$?\s*([\d.,]+)"
    match = re.search(padrao, texto, re.IGNORECASE)
    if match:
        valor_str = match.group(1).replace(".", "").replace(",", ".")
        try:
            return float(valor_str)
        except ValueError:
            pass
    return None


def extrair_codigo_barras(texto: str) -> str | None:
    """Extrai código de barras numérico (44-48 dígitos)."""
    padrao = r"\b(\d[\d\s.]{40,55})\b"
    match = re.search(padrao, texto)
    if match:
        codigo = re.sub(r"[\s.]", "", match.group(1))
        if 44 <= len(codigo) <= 48:
            return codigo
    return None


def _para_float_valor(s: str) -> float:
    try:
        return float(s.replace(".", "").replace(",", "."))
    except Exception:
        return 0.0


def _tem_valor_a_pagar(texto_upper: str) -> bool:
    """True se o recibo tem QUALQUER valor > 0 em contexto de pagar/recolher/débito.
    Serve pra VENCER os indicadores fracos de 'sem movimento': um '0,00' avulso no
    EFD-Contribuições, ou um 'COFINS Sem Movimento' por tributo num DCTFWeb que tem
    débito de Previdenciária (ex.: linha 'TOTAL R$ 502,51')."""
    padroes = [
        r"\bTOTAL\b\D{0,12}?R\$\s*([\d.]+,\d{2})",
        r"A\s+RECOLHER\D{0,12}?R\$\s*([\d.]+,\d{2})",
        r"SALDO\s+A\s+PAGAR\D{0,12}?R\$\s*([\d.]+,\d{2})",
        r"D[EÉ]BITO[S]?\s+APURAD\w*\D{0,12}?R\$\s*([\d.]+,\d{2})",
        r"VALOR\s+DO\s+DOCUMENTO\D{0,12}?R\$\s*([\d.]+,\d{2})",
    ]
    for pat in padroes:
        for m in re.findall(pat, texto_upper):
            if _para_float_valor(m) > 0:
                return True
    return False


def _detectar_sem_movimento(tipo_guia: str | None, valor: float | None, texto_pdf: str) -> bool:
    """Detecta se o documento é um recibo/guia SEM MOVIMENTO.
    Critérios:
    0. PRIORIDADE: se há valor REAL a recolher/pagar > 0 → NÃO é sem movimento (vence tudo).
    1. Recibo de transmissão de DAS/DCTFWEB/DEFIS com valor 0,00 ou sem valor
    2. Texto explícito: 'SEM MOVIMENTO', 'NADA A RECOLHER', 'NÃO HÁ VALOR'
    3. Guia de pagamento (DAS, DARF, GPS, etc.) com valor == 0.00
    """
    texto_upper = texto_pdf[:6000].upper()

    # Critério 0 (2026-08): valor REAL a pagar > 0 vence os indicadores fracos.
    # Conserta EFD-Contrib (tem '0,00' avulso mas também 'a recolher R$ 422,50') e
    # DCTFWeb (tem 'COFINS Sem Movimento' por tributo mas 'TOTAL R$ 502,51' de débito).
    if (valor is not None and valor > 0) or _tem_valor_a_pagar(texto_upper):
        return False

    # Critério 1: texto explícito de sem movimento
    indicadores_sem_movimento = [
        r"SEM\s+MOVIMENTO",
        r"NADA\s+A\s+RECOLHER",
        r"N[AÃ]O\s+H[AÁ]\s+(?:VALOR|TRIBUTO|D[EÉ]BITO)",
        r"VALOR\s+(?:TOTAL\s+)?(?:A\s+)?(?:PAGAR|RECOLHER)\s*[:\s=]*R?\$?\s*0+[.,]0{2}",
        r"VALOR\s+DO\s+DOCUMENTO\s*[:\s=]*R?\$?\s*0+[.,]0{2}",
        r"TOTAL\s*[:\s=]*R?\$?\s*0+[.,]0{2}",
        r"\bR\$\s*0,00\b",
        r"RECEITA\s+BRUTA.*0,00",
    ]
    texto_diz_sem_movimento = False
    for padrao in indicadores_sem_movimento:
        if re.search(padrao, texto_upper):
            texto_diz_sem_movimento = True
            break

    # Critério 2: é um tipo de guia de pagamento com valor zero ou None
    tipos_pagamento = {"DAS", "DAS-MEI", "DARF", "GPS", "GRF-FGTS", "ISS", "ICMS"}
    eh_guia_pagamento = tipo_guia in tipos_pagamento

    valor_zerado = valor is None or valor == 0 or valor == 0.0

    # Caso 1: texto diz explicitamente que é sem movimento
    if texto_diz_sem_movimento:
        return True

    # Caso 2: guia de pagamento com valor zerado + é recibo de transmissão
    eh_recibo = bool(re.search(r"RECIBO\s+DE\s+(?:ENTREGA|TRANSMISS)", texto_upper))
    if eh_guia_pagamento and valor_zerado and eh_recibo:
        return True

    # Caso 3: DAS/DAS-MEI com valor explicitamente 0,00 (mesmo se não é recibo)
    if tipo_guia in {"DAS", "DAS-MEI"} and valor is not None and valor == 0.0:
        return True

    return False


def identificar_tipo_guia_por_nome(nome_arquivo: str) -> str | None:
    """Tenta identificar o tipo de guia pelo nome do arquivo."""
    nome_upper = nome_arquivo.upper()
    # Ordem importa: DEFIS antes de DAS para não confundir
    for regra in REGRAS_GUIAS:
        for desc in regra["descricoes_match"]:
            if desc.upper() in nome_upper:
                return regra["tipo"]
    return None


def identificar_tipo_guia_por_pasta(caminho_pdf: Path) -> str | None:
    """Identifica tipo de guia pelo nome da subpasta onde o PDF está.
    Ex: /DESTDA 02-2026/recibo.pdf → 'DESTDA'
        /DEFIS 2025/recibo.pdf → 'DEFIS'
    """
    pasta_pai = caminho_pdf.parent.name.upper()
    # Ordem importa: DEFIS/DEISS antes de DAS/ISS para não confundir parciais
    for regra in REGRAS_GUIAS:
        tipo_upper = regra["tipo"].upper()
        if tipo_upper in pasta_pai:
            return regra["tipo"]
    return None


def identificar_tipo_guia(texto: str, nome_arquivo: str = "", caminho_pdf: Path | None = None) -> str | None:
    """Identifica o tipo de guia pela subpasta, nome do arquivo e conteúdo do PDF."""
    # 1. Tenta pelo nome da subpasta (mais confiável — o usuário organizou assim)
    if caminho_pdf:
        tipo_por_pasta = identificar_tipo_guia_por_pasta(caminho_pdf)
        if tipo_por_pasta:
            return tipo_por_pasta

    # 2. Tenta pelo nome do arquivo
    tipo_por_nome = identificar_tipo_guia_por_nome(nome_arquivo)
    if tipo_por_nome:
        return tipo_por_nome

    # 3. Tenta pelo conteúdo do PDF
    texto_upper = texto.upper()
    for regra in REGRAS_GUIAS:
        for marcador in regra["textos_pdf"]:
            if marcador.upper() in texto_upper:
                return regra["tipo"]
    return None


def _eh_guia_anual(tipo_guia: str) -> bool:
    """Verifica se o tipo de guia é anual (ex: DEFIS)."""
    regra = next((r for r in REGRAS_GUIAS if r["tipo"] == tipo_guia), None)
    return regra.get("anual", False) if regra else False


def _extrair_competencia_nome_arquivo(nome: str) -> str | None:
    """Tenta extrair competência MM-YYYY ou MM/YYYY do nome do arquivo.
    Ex: 'recibo efd contribuicoes jacobi 02-2026.pdf' → '2026-02'
        'Recibo_53283348000108_032026_...' → '2026-03'
    """
    # Padrão: MM-YYYY ou MM/YYYY
    match = re.search(r"(\d{2})[-/](\d{4})", nome)
    if match:
        mes, ano = match.group(1), match.group(2)
        if 1 <= int(mes) <= 12 and 2020 <= int(ano) <= 2030:
            return f"{ano}-{mes}"
    # Padrão: YYYY-MM ou YYYY/MM
    match = re.search(r"(\d{4})[-/](\d{2})", nome)
    if match:
        ano, mes = match.group(1), match.group(2)
        if 1 <= int(mes) <= 12 and 2020 <= int(ano) <= 2030:
            return f"{ano}-{mes}"
    # Padrão: MMYYYY colado (ex: _032026_ no nome do arquivo)
    match = re.search(r"_(\d{2})(202[0-9])_", nome)
    if match:
        mes, ano = match.group(1), match.group(2)
        if 1 <= int(mes) <= 12:
            return f"{ano}-{mes}"
    return None


def analisar_pdf(caminho_pdf: Path) -> dict:
    """Analisa um PDF e retorna os dados extraídos."""
    texto = extrair_texto_pdf(caminho_pdf)

    if not texto.strip():
        return {"erro": "PDF sem texto (imagem) e OCR não conseguiu extrair"}

    tipo_guia = identificar_tipo_guia(texto, caminho_pdf.name, caminho_pdf)
    cnpj = extrair_cnpj(texto)
    valor = extrair_valor(texto)
    vencimento = extrair_vencimento(texto)
    codigo_barras = extrair_codigo_barras(texto)

    # Para guias anuais conhecidas (DEFIS, etc.), extrai só o ano
    if tipo_guia and _eh_guia_anual(tipo_guia):
        competencia = extrair_ano_calendario(texto, caminho_pdf.name)
        competencia_alt = None
    else:
        competencia = extrair_competencia(texto)
        # Fallback: tenta extrair do nome do arquivo (ex: "jacobi 02-2026.pdf")
        if not competencia:
            competencia = _extrair_competencia_nome_arquivo(caminho_pdf.name)
        competencia_anual = extrair_ano_calendario(texto, caminho_pdf.name)
        # Se tipo desconhecido E achou tanto mensal quanto anual, guarda ambas
        # O match genérico vai tentar com a segunda se a primeira falhar
        if not tipo_guia and competencia and competencia_anual and competencia_anual != competencia:
            competencia_alt = competencia_anual
        elif not competencia and competencia_anual:
            competencia = competencia_anual
            competencia_alt = None
        else:
            competencia_alt = None

    # Detectar sem movimento (DAS R$0, recibos zerados, texto explícito)
    sem_movimento = _detectar_sem_movimento(tipo_guia, valor, texto[:5000])

    return {
        "tipo_guia": tipo_guia,
        "cnpj": cnpj,
        "competencia": competencia,
        "competencia_alt": competencia_alt,  # competência alternativa (anual vs mensal)
        "valor": valor,
        "vencimento": vencimento,            # data de vencimento do boleto (ISO), pra ficar pronto pra enviar
        "codigo_barras": codigo_barras,
        "texto_completo": texto[:5000],
        "sem_movimento": sem_movimento,
    }


# ============================================================
# INTEGRAÇÃO SUPABASE
# ============================================================


def buscar_cliente_por_cnpj(cnpj: str) -> dict | None:
    """Busca cliente no Supabase pelo CNPJ (tenta com e sem formatação)."""
    # Tenta com formatação original
    resp = (
        supabase.table("clientes")
        .select("id, razaoSocial, cnpj, regimeTributario")
        .eq("cnpj", cnpj)
        .limit(1)
        .execute()
    )
    if resp.data:
        return resp.data[0]

    # Tenta só com dígitos
    cnpj_limpo = limpar_cnpj(cnpj)
    resp = (
        supabase.table("clientes")
        .select("id, razaoSocial, cnpj, regimeTributario")
        .eq("cnpj", cnpj_limpo)
        .limit(1)
        .execute()
    )
    if resp.data:
        return resp.data[0]
    return None


def _match_descricao_exato(descricao: str, termo: str) -> bool:
    """Verifica se o termo aparece como PALAVRA INTEIRA na descrição do evento.
    Ex: termo='DAS' casa com 'DAS - Simples Nacional' mas NÃO com 'Fechados',
    'Lançamento das notas' ou 'DAS-MEI'.
    Usa lookbehind/lookahead incluindo hífens para evitar falsos positivos."""
    # Normaliza removendo acentos
    desc_norm = unicodedata.normalize("NFKD", descricao).replace("\u00ba", "o").replace("\u00aa", "a")
    desc_norm = "".join(c for c in desc_norm if not unicodedata.combining(c))
    termo_norm = unicodedata.normalize("NFKD", termo).replace("\u00ba", "o").replace("\u00aa", "a")
    termo_norm = "".join(c for c in termo_norm if not unicodedata.combining(c))

    # Para termos curtos (<=4 chars como DAS, GPS, ISS), faz match CASE-SENSITIVE
    # para evitar que "das" (preposição) case com "DAS" (sigla)
    if len(termo_norm) <= 4 and termo_norm.isupper():
        texto_busca = desc_norm  # preserva case original
    else:
        texto_busca = desc_norm.upper()
        termo_norm = termo_norm.upper()

    # Escapa caracteres especiais de regex no termo
    termo_escaped = re.escape(termo_norm)
    # Se o termo contém hífen/barra (EFD-CONTRIBUICOES), permite variações
    if "-" in termo or "/" in termo:
        termo_pattern = termo_escaped.replace(r"\-", r"[\s\-/]").replace(r"\/", r"[\s\-/]")
    else:
        termo_pattern = termo_escaped

    # Lookbehind e lookahead incluem hífens para que DAS não case com DAS-MEI
    return bool(re.search(r"(?<![A-Za-z0-9\-])" + termo_pattern + r"(?![A-Za-z0-9\-])", texto_busca))


def _filtrar_matches_validos(candidatos: list[dict], descricoes: list[str]) -> list[dict]:
    """Filtra candidatos mantendo apenas os que têm match exato (word boundary) com alguma descrição.
    Retorna lista de matches válidos (pode ser 0, 1 ou N)."""
    validos = []
    for evento in candidatos:
        desc_evento = evento.get("descricao", "")
        for termo in descricoes:
            if _match_descricao_exato(desc_evento, termo):
                validos.append(evento)
                break  # Não duplicar mesmo evento
    return validos


def _eh_competencia_so_ano(competencia: str) -> bool:
    """Verifica se a competência é só ano (ex: '2026' sem mês)."""
    return bool(competencia and re.match(r'^\d{4}$', competencia))


def _aplicar_filtro_competencia(query, competencia: str):
    """Aplica filtro de competência à query do Supabase.
    Se a competência é só ano (4 dígitos), usa LIKE 'YYYY-%' para pegar qualquer mês.
    Caso contrário, usa eq exato."""
    if _eh_competencia_so_ano(competencia):
        return query.like("competencia", f"{competencia}-%")
    return query.eq("competencia", competencia)


def buscar_obrigacao_por_cliente_id(cliente_id: int, tipo_guia: str, competencia: str) -> dict | None:
    """
    Busca principal (ERP): match exato por cliente_id (FK) + descricao + competencia.
    Este é o caminho mais confiável: PDF → CNPJ → clientes.id → eventos_rotina.cliente_id.
    SEGURANÇA: exige match de palavra inteira e resultado ÚNICO (sem ambiguidade).
    """
    regra = next((r for r in REGRAS_GUIAS if r["tipo"] == tipo_guia), None)
    descricoes = regra["descricoes_match"] if regra else [tipo_guia]

    # Busca todos os candidatos (sem limit) para validar unicidade
    todos_candidatos = []
    for desc in descricoes:
        q = (
            supabase.table("eventos_rotina")
            .select("*")
            .eq("cliente_id", cliente_id)
            .ilike("descricao", f"%{desc}%")
        )
        q = _aplicar_filtro_competencia(q, competencia)
        resp = q.eq("realizada", False).execute()
        for evt in (resp.data or []):
            if evt["id"] not in {c["id"] for c in todos_candidatos}:
                todos_candidatos.append(evt)

    if not todos_candidatos:
        return None

    # Filtrar: só aceita matches com word-boundary exato
    validos = _filtrar_matches_validos(todos_candidatos, descricoes)

    if len(validos) == 1:
        return validos[0]
    elif len(validos) > 1:
        descs = [f"#{e['id']} '{e.get('descricao')}'" for e in validos]
        logger.warning(f"   ⚠️ AMBIGUIDADE: {len(validos)} tarefas encontradas para {tipo_guia}: {descs} → recusando match")
        return None
    else:
        # Nenhum match válido por word-boundary
        logger.debug(f"   Nenhum match word-boundary para {tipo_guia} entre {len(todos_candidatos)} candidatos")
        return None


def buscar_obrigacao_por_cnpj_direto(cnpj: str, tipo_guia: str, competencia: str) -> dict | None:
    """
    Fallback 1: busca pela coluna cnpj da eventos_rotina (sem passar pela tabela clientes).
    SEGURANÇA: exige match de palavra inteira e resultado ÚNICO.
    """
    cnpj_limpo = limpar_cnpj(cnpj)
    regra = next((r for r in REGRAS_GUIAS if r["tipo"] == tipo_guia), None)
    descricoes = regra["descricoes_match"] if regra else [tipo_guia]

    todos_candidatos = []
    for cnpj_busca in [cnpj, cnpj_limpo]:
        for desc in descricoes:
            q = (
                supabase.table("eventos_rotina")
                .select("*")
                .eq("cnpj", cnpj_busca)
                .ilike("descricao", f"%{desc}%")
            )
            q = _aplicar_filtro_competencia(q, competencia)
            resp = q.eq("realizada", False).execute()
            for evt in (resp.data or []):
                if evt["id"] not in {c["id"] for c in todos_candidatos}:
                    todos_candidatos.append(evt)

    if not todos_candidatos:
        return None

    validos = _filtrar_matches_validos(todos_candidatos, descricoes)

    if len(validos) == 1:
        return validos[0]
    elif len(validos) > 1:
        descs = [f"#{e['id']} '{e.get('descricao')}'" for e in validos]
        logger.warning(f"   ⚠️ AMBIGUIDADE (cnpj): {len(validos)} tarefas para {tipo_guia}: {descs} → recusando match")
        return None
    return None


def buscar_obrigacao_por_nome(nome_cliente: str, tipo_guia: str, competencia: str) -> dict | None:
    """
    Fallback 2: busca por nome do cliente (ILIKE) + descricao + competencia.
    Menos confiável, usado quando cliente_id e cnpj falham.
    SEGURANÇA: exige match de palavra inteira e resultado ÚNICO.
    """
    regra = next((r for r in REGRAS_GUIAS if r["tipo"] == tipo_guia), None)
    descricoes = regra["descricoes_match"] if regra else [tipo_guia]

    todos_candidatos = []
    for desc in descricoes:
        q = (
            supabase.table("eventos_rotina")
            .select("*")
            .ilike("cliente", f"%{nome_cliente}%")
            .ilike("descricao", f"%{desc}%")
        )
        q = _aplicar_filtro_competencia(q, competencia)
        resp = q.eq("realizada", False).execute()
        for evt in (resp.data or []):
            if evt["id"] not in {c["id"] for c in todos_candidatos}:
                todos_candidatos.append(evt)

    if not todos_candidatos:
        return None

    validos = _filtrar_matches_validos(todos_candidatos, descricoes)

    if len(validos) == 1:
        return validos[0]
    elif len(validos) > 1:
        descs = [f"#{e['id']} '{e.get('descricao')}'" for e in validos]
        logger.warning(f"   ⚠️ AMBIGUIDADE (nome): {len(validos)} tarefas para {tipo_guia}: {descs} → recusando match")
        return None
    return None


def buscar_obrigacao_por_descricao(cliente_id, cnpj, descricao_evento: str, competencia: str) -> dict | None:
    """Busca obrigação pendente pela DESCRIÇÃO aprendida na triagem + competência.
    Usado pelo aprendizado (Forma 1). SEGURANÇA: só aceita resultado ÚNICO."""
    if not descricao_evento:
        return None
    candidatos: list[dict] = []
    if cliente_id:
        q = (
            supabase.table("eventos_rotina")
            .select("*")
            .eq("cliente_id", cliente_id)
            .ilike("descricao", f"%{descricao_evento}%")
        )
        q = _aplicar_filtro_competencia(q, competencia)
        resp = q.eq("realizada", False).execute()
        candidatos = resp.data or []
    elif cnpj:
        cnpj_limpo = limpar_cnpj(cnpj)
        for cnpj_busca in [cnpj, cnpj_limpo]:
            q = (
                supabase.table("eventos_rotina")
                .select("*")
                .eq("cnpj", cnpj_busca)
                .ilike("descricao", f"%{descricao_evento}%")
            )
            q = _aplicar_filtro_competencia(q, competencia)
            resp = q.eq("realizada", False).execute()
            for evt in (resp.data or []):
                if evt["id"] not in {c["id"] for c in candidatos}:
                    candidatos.append(evt)
    if len(candidatos) == 1:
        return candidatos[0]
    if len(candidatos) > 1:
        logger.warning(f"   ⚠️ AMBIGUIDADE (aprendizado): {len(candidatos)} p/ '{descricao_evento}' → recusando match")
    return None


def identificar_por_aprendizado(texto_pdf: str, cnpj: str | None, cliente_id: int | None) -> str | None:
    """Consulta as aulas da triagem: acha o exemplo cujo texto mais se parece com o PDF
    atual e devolve a descrição da obrigação que um humano confirmou pra ele.
    Prioriza exemplos do mesmo cliente/CNPJ. Reusa a tokenização que já existe."""
    if not _APRENDIZADO or not texto_pdf:
        return None
    cnpj_limpo = limpar_cnpj(cnpj) if cnpj else None
    # Prioriza exemplos da mesma empresa; se não houver, compara com todos
    mesmos = [
        e for e in _APRENDIZADO
        if (cliente_id and e.get("cliente_id") == cliente_id)
        or (cnpj_limpo and limpar_cnpj(e.get("cnpj") or "") == cnpj_limpo)
    ]
    pool = mesmos if mesmos else _APRENDIZADO

    tokens_pdf = _tokenizar(texto_pdf[:3000])
    if not tokens_pdf:
        return None

    melhor_score = 0.0
    melhor_desc = None
    for ex in pool:
        amostra = ex.get("texto_amostra") or ""
        if not amostra:
            continue
        tokens_ex = _tokenizar(amostra[:3000])
        if not tokens_ex:
            continue
        # Jaccard: interseção / união (parecença entre dois textos de documento)
        inter = len(tokens_pdf & tokens_ex)
        union = len(tokens_pdf | tokens_ex) or 1
        score = inter / union
        if score > melhor_score:
            melhor_score = score
            melhor_desc = ex.get("descricao_evento")

    # Limiar conservador: precisa de parecença real pra adotar a aula
    LIMIAR = 0.40
    if melhor_desc and melhor_score >= LIMIAR:
        logger.info(f"   Aprendizado: melhor parecença {melhor_score:.2f} → '{melhor_desc}'")
        return melhor_desc
    return None


def upload_pdf(caminho_pdf: Path, cliente_nome: str, descricao: str) -> str | None:
    """Faz upload do PDF para o Supabase Storage e retorna a URL pública."""
    nome_arquivo = (
        f"{cliente_nome}_{descricao}_{int(time.time())}"
        .replace(" ", "_")
        .replace("/", "_")
        .replace("°", "")
        .replace("º", "")
    )
    # Remove acentos (ex: Ç→C, Ã→A, É→E) para evitar InvalidKey no Supabase Storage
    nome_arquivo = unicodedata.normalize("NFKD", nome_arquivo).encode("ascii", "ignore").decode("ascii")
    nome_arquivo = re.sub(r"[^\w_-]", "", nome_arquivo) + ".pdf"
    storage_path = f"{CONFIG['storage_path_prefix']}/{nome_arquivo}"

    try:
        with open(caminho_pdf, "rb") as f:
            supabase.storage.from_(CONFIG["bucket_storage"]).upload(
                storage_path,
                f.read(),
                file_options={
                    "cache-control": "3600",
                    "upsert": "true",
                    "content-type": "application/pdf",
                },
            )
    except Exception as e:
        logger.error(f"   Falha no upload do PDF ({storage_path}): {e}")
        return None

    url_resp = supabase.storage.from_(CONFIG["bucket_storage"]).get_public_url(storage_path)
    return url_resp


def dar_baixa_obrigacao(evento: dict, pdf_url: str, dados: dict) -> bool:
    """Atualiza a obrigação no Supabase: marca como realizada e anexa o PDF."""
    vencimento = evento.get("vencimento")
    hoje = date.today().isoformat()

    # Determina se está em atraso
    if vencimento and hoje > vencimento:
        situacao = "Realizada em Atraso"
    else:
        situacao = "Realizada no Prazo"

    # Detecta se é guia de pagamento: primeiro tenta pela lista rápida,
    # senão analisa o texto do PDF por padrões estruturais (genérico)
    tipos_pagamento_conhecidos = {"DAS", "DAS-MEI", "DARF", "GPS", "GRF-FGTS", "ISS", "ICMS"}
    tipo = dados.get("tipo_guia", "")
    if tipo in tipos_pagamento_conhecidos:
        eh_guia_pagamento = True
    else:
        eh_guia_pagamento = _eh_guia_pagamento_por_texto(dados.get("texto_completo", ""))

    # Detectar sem movimento: se é sem movimento, NÃO precisa enviar ao cliente
    sem_movimento = dados.get("sem_movimento", False)

    _nome_usr = CONFIG.get("user_nome") or "Agente"
    update_data = {
        "situacao": situacao,
        "realizada": True,
        "data_realizada": datetime.now().strftime("%d/%m/%Y"),
        "pdf_url": pdf_url,
        "origem_baixa": "agente",
        "pendente_envio": eh_guia_pagamento and not sem_movimento,
        "semMovimento": sem_movimento,
        # Autoria: a baixa do agente conta como feita por quem é dono do agente (aparece pra todos)
        "finalizado_por_id": USER_ID,
        "finalizado_por_nome": _nome_usr,
        "finalizado_em": datetime.now().isoformat(),
        "editado_por_id": USER_ID,
        "editado_por_nome": _nome_usr,
        "editado_em": datetime.now().isoformat(),
    }

    # Campos opcionais
    if dados.get("valor"):
        update_data["valor"] = dados["valor"]
    if dados.get("codigo_barras"):
        update_data["codigo_boleto"] = dados["codigo_barras"]
    if dados.get("tipo_guia"):
        regra = next((r for r in REGRAS_GUIAS if r["tipo"] == dados["tipo_guia"]), None)
        update_data["tipo_guia"] = regra["tipo_guia_banco"] if regra else dados["tipo_guia"]
    if dados.get("competencia"):
        # Para anuais (ex: "2025"), competencia_guia = "2025"; mensal ("2025-03") = "03/2025"
        partes = dados["competencia"].split("-")
        if len(partes) == 2:
            update_data["competencia_guia"] = f"{partes[1]}/{partes[0]}"
        else:
            update_data["competencia_guia"] = dados["competencia"]

    resp = (
        supabase.table("eventos_rotina")
        .update(update_data)
        .eq("id", evento["id"])
        .execute()
    )

    # Histórico: registra a baixa com o teu nome (aparece pra todos, mesmo se outro PC tiver outro agente)
    if resp.data:
        try:
            _tg = dados.get("tipo_guia") or "documento"
            _msg = f"Baixa automática pelo agente — {_tg} anexado e marcado como {situacao.lower()}"
            supabase.table("historico_eventos_rotina").insert({
                "registro_id": evento["id"],
                "mensagem": _msg,
                "usuario": _nome_usr,
                "tipo": "alteracao",
                "data_hora": datetime.now().isoformat(),
            }).execute()
        except Exception as _e:
            logger.warning(f"   Falha ao registrar histórico da baixa: {_e}")

        # Vencimento do boleto: update separado e protegido (se a coluna não existir, NÃO quebra a baixa)
        if dados.get("vencimento"):
            try:
                supabase.table("eventos_rotina").update(
                    {"vencimento_guia": dados["vencimento"]}
                ).eq("id", evento["id"]).execute()
                logger.info(f"   Vencimento do boleto salvo: {dados['vencimento']}")
            except Exception as _e:
                logger.warning(f"   Não consegui gravar vencimento_guia (coluna pode não existir): {_e}")

    return bool(resp.data)


# ============================================================
# PROCESSAMENTO DE ARQUIVO
# ============================================================


def _extrair_nome_cliente_do_arquivo(nome_arquivo: str) -> str | None:
    """Extrai nome do cliente a partir do nome do arquivo.
    Padrões reconhecidos:
      'recibo destda 02-2026 borre e de paula.pdf' → 'borre e de paula'
      'recibo efd contribuicoes jacobi 02-2026.pdf' → 'jacobi'
      'DAS 03-2026 padaria silva.pdf' → 'padaria silva'
    Remove: prefixos de data (2026-04-16_), tipo de guia, competência (MM-YYYY, YYYY-MM, MMYYYY).
    """
    nome = Path(nome_arquivo).stem
    # Remove prefixos de data empilhados (2026-03-17_2026-03-17_...)
    nome = re.sub(r'(\d{4}-\d{2}-\d{2}_)+', '', nome)
    # Remove "recibo" do início
    nome = re.sub(r'^recibo\s+', '', nome, flags=re.IGNORECASE)
    # Remove rótulos de documento que NÃO são o cliente (ex: "Protocolo Entrega DME ... <cliente>")
    for _lixo in ("protocolo de entrega", "protocolo entrega", "protocolo",
                  "entrega", "comprovante", "declaracao", "declaração",
                  "transmissao", "transmissão"):
        nome = re.sub(rf'\b{_lixo}\b', '', nome, flags=re.IGNORECASE)
    # Remove tipos de guia conhecidos
    for regra in REGRAS_GUIAS:
        tipo = regra["tipo"]
        nome = re.sub(rf'\b{re.escape(tipo)}\b', '', nome, flags=re.IGNORECASE)
        for desc in regra.get("descricoes_match", []):
            nome = re.sub(rf'\b{re.escape(desc)}\b', '', nome, flags=re.IGNORECASE)
    # Remove competências (02-2026, 2026-02, 032026, etc.)
    nome = re.sub(r'\b\d{2}[-/]\d{4}\b', '', nome)
    nome = re.sub(r'\b\d{4}[-/]\d{2}\b', '', nome)
    nome = re.sub(r'\b\d{6}\b', '', nome)  # MMYYYY colado
    # Remove underscores e hífens soltos, espaços extras
    nome = re.sub(r'[_\-]+', ' ', nome)
    nome = re.sub(r'\s+', ' ', nome).strip()
    # Ignora se restou muito curto (< 3 chars)
    if len(nome) < 3:
        return None
    return nome


def _buscar_cliente_por_nome(nome: str) -> dict | None:
    """Busca cliente no Supabase por nome (ILIKE), retorna o mais parecido."""
    from difflib import SequenceMatcher
    palavras = nome.split()
    if not palavras:
        return None
    # Usa primeira e última palavra para busca ILIKE
    primeira = palavras[0]
    ultima = palavras[-1] if len(palavras) > 1 else palavras[0]
    resp = (
        supabase.table("clientes")
        .select("id, razaoSocial, cnpj")
        .ilike("razaoSocial", f"%{primeira}%{ultima}%")
        .limit(10)
        .execute()
    )
    if not resp.data:
        # Tenta só com a primeira palavra (pode ser sobrenome único)
        resp = (
            supabase.table("clientes")
            .select("id, razaoSocial, cnpj")
            .ilike("razaoSocial", f"%{primeira}%")
            .limit(10)
            .execute()
        )
    if not resp.data:
        return None
    # Ranqueia por similaridade
    melhor = max(resp.data, key=lambda c: SequenceMatcher(
        None, nome.upper(), c["razaoSocial"].upper()
    ).ratio())
    score = SequenceMatcher(None, nome.upper(), melhor["razaoSocial"].upper()).ratio()
    # SEGURANÇA: além do score, exige que uma palavra significativa do nome (>=4 letras)
    # apareça na razão social do cliente. Evita casar "elvis kumm" com "Ribeiro..." por acaso.
    razao_up = melhor["razaoSocial"].upper()
    palavras_sig = [p for p in nome.upper().split() if len(p) >= 4]
    tem_palavra = any(p in razao_up for p in palavras_sig) if palavras_sig else False
    if score >= 0.55 and tem_palavra:
        logger.info(f"   _buscar_cliente_por_nome: '{nome}' → '{melhor['razaoSocial']}' (score={score:.2f})")
        return melhor
    logger.info(f"   _buscar_cliente_por_nome: '{nome}' SEM match seguro (melhor='{melhor['razaoSocial']}' score={score:.2f}) → recusando")
    return None


def processar_arquivo(caminho_pdf: Path) -> None:
    """Pipeline completo: ler PDF → identificar → buscar → copiar."""
    nome = caminho_pdf.name

    # DEFESA 1: ignora se está em pasta interna (redundante com on_created, mas seguro)
    if _esta_em_pasta_interna(caminho_pdf):
        return

    # DEFESA 2: detecta prefixo de data duplo (sinal de loop infinito)
    if re.match(r'\d{4}-\d{2}-\d{2}_\d{4}-\d{2}-\d{2}_', nome):
        logger.warning(f"[AVISO] Prefixo de data empilhado detectado (loop?): {nome}")
        return

    # Ignora se já foi processado (checa nome completo E nome-base sem prefixo)
    nome_base = _nome_base(nome)
    if nome in _arquivos_processados or nome_base in _arquivos_processados:
        return

    logger.info(f"[PDF] Novo arquivo detectado: {nome}")

    try:
        # 1. Extrair dados do PDF
        dados = analisar_pdf(caminho_pdf)

        if dados.get("erro"):
            logger.warning(f"[AVISO] {nome}: {dados['erro']}")
            _copiar_para_erro(caminho_pdf, dados["erro"], dados)
            return

        sem_mov_tag = " | SEM MOVIMENTO" if dados.get("sem_movimento") else ""
        logger.info(
            f"   Tipo: {dados['tipo_guia']} | CNPJ: {dados['cnpj']} | "
            f"Competência: {dados['competencia']} | Valor: {dados['valor']}{sem_mov_tag}"
        )

        # 2. Validações mínimas
        # 2b. Check de permissão: este CNPJ pertence aos clientes deste usuário?
        if dados["cnpj"] and not _cnpj_permitido(dados["cnpj"]):
            logger.info(f"   CNPJ {dados['cnpj']} não pertence a este usuário — ignorando")
            return

        # 3. Buscar cliente
        razao_social = None
        obrigacao = None
        cliente_id = None

        if dados["cnpj"]:
            cliente = buscar_cliente_por_cnpj(dados["cnpj"])
            if cliente:
                razao_social = cliente["razaoSocial"]
                cliente_id = cliente["id"]
                logger.info(f"   Cliente encontrado: {razao_social} (id={cliente_id})")

        # 3b. Fallback: sem CNPJ, tenta o NOME DO ARQUIVO primeiro (fonte curada pelo usuário:
        #     "Protocolo Entrega DME 05-2026 elvis kumm.pdf" → "elvis kumm")
        if not dados["cnpj"] and not razao_social:
            nome_extraido = _extrair_nome_cliente_do_arquivo(caminho_pdf.name)
            if nome_extraido:
                razao_social = nome_extraido
                logger.info(f"   CNPJ ausente — nome extraído do arquivo: {nome_extraido}")

        # 3c. Fallback: se o arquivo não deu nome, tenta extrair do texto OCR (menos confiável)
        if not dados["cnpj"] and not razao_social:
            nome_extraido = extrair_nome_empresa(dados.get("texto_completo", ""))
            if nome_extraido:
                razao_social = nome_extraido
                logger.info(f"   CNPJ ausente — nome extraído do PDF: {nome_extraido}")

        # 3d. Com nome (de qualquer fonte), tenta achar o cliente na tabela
        if not cliente_id and razao_social:
            cliente_encontrado = _buscar_cliente_por_nome(razao_social)
            if cliente_encontrado:
                cliente_id = cliente_encontrado["id"]
                razao_social = cliente_encontrado["razaoSocial"]
                dados["cnpj"] = cliente_encontrado.get("cnpj")
                logger.info(f"   Cliente por nome: {razao_social} (id={cliente_id})")

        # 4. Buscar obrigação — cascata de estratégias
        tipo_guia = dados["tipo_guia"]
        competencia = dados["competencia"]

        # Estratégia A: REGRAS_GUIAS (fast-path, tipos conhecidos)
        match_confianca = None  # "alta", "media", "baixa"
        if tipo_guia and competencia:

            if cliente_id:
                obrigacao = buscar_obrigacao_por_cliente_id(cliente_id, tipo_guia, competencia)
                if obrigacao:
                    match_confianca = "alta"
                    logger.info(f"   Match por REGRAS_GUIAS + cliente_id={cliente_id} [confiança: ALTA]")

            if not obrigacao and dados["cnpj"]:
                obrigacao = buscar_obrigacao_por_cnpj_direto(dados["cnpj"], tipo_guia, competencia)
                if obrigacao:
                    match_confianca = "media"
                    razao_social = obrigacao.get("cliente", razao_social)
                    logger.info(f"   Match por REGRAS_GUIAS + cnpj direto [confiança: MÉDIA]")

            if not obrigacao and razao_social:
                obrigacao = buscar_obrigacao_por_nome(razao_social, tipo_guia, competencia)
                if obrigacao:
                    match_confianca = "baixa"
                    logger.info(f"   Match por REGRAS_GUIAS + nome [confiança: BAIXA]")

        # Estratégia A.5: APRENDIZADO (tipos ensinados na triagem — Forma 1)
        if not obrigacao and competencia:
            descricao_aprendida = identificar_por_aprendizado(
                dados.get("texto_completo", ""), dados["cnpj"], cliente_id
            )
            if descricao_aprendida:
                logger.info(f"   Aprendizado reconheceu: '{descricao_aprendida}'")
                if cliente_id:
                    obrigacao = buscar_obrigacao_por_descricao(cliente_id, None, descricao_aprendida, competencia)
                    if obrigacao:
                        match_confianca = "alta"
                        if not dados.get("tipo_guia"):
                            dados["tipo_guia"] = obrigacao.get("descricao")
                        logger.info("   Match por APRENDIZADO + cliente_id [confiança: ALTA]")
                if not obrigacao and dados["cnpj"]:
                    obrigacao = buscar_obrigacao_por_descricao(None, dados["cnpj"], descricao_aprendida, competencia)
                    if obrigacao:
                        match_confianca = "media"
                        if not dados.get("tipo_guia"):
                            dados["tipo_guia"] = obrigacao.get("descricao")
                        logger.info("   Match por APRENDIZADO + cnpj [confiança: MÉDIA]")

        # Estratégia B: MATCHING GENÉRICO (para tipos NÃO treinados — DIMOB, DMED, IBAMA, etc.)
        if not obrigacao and competencia:
            texto_pdf = dados.get("texto_completo", "")
            logger.info(f"   REGRAS_GUIAS não bateu (tipo={tipo_guia}). Tentando match genérico...")
            obrigacao = buscar_obrigacao_generica(
                cliente_id, dados["cnpj"], razao_social, competencia, texto_pdf
            )
            if obrigacao:
                match_confianca = "baixa"
                dados["tipo_guia"] = obrigacao.get("descricao", tipo_guia or "DOCUMENTO")
                logger.info(f"   Match GENÉRICO: '{obrigacao.get('descricao')}' (id={obrigacao['id']}) [confiança: BAIXA]")

        # Estratégia C: competência alternativa (ex: PDF anual "Relatório: 2026/2025" → tenta 2025)
        competencia_alt = dados.get("competencia_alt")
        if not obrigacao and competencia_alt:
            logger.info(f"   Tentando competência alternativa: {competencia_alt}...")
            texto_pdf = dados.get("texto_completo", "")

            # Tenta REGRAS_GUIAS com competência alternativa
            if tipo_guia and cliente_id:
                obrigacao = buscar_obrigacao_por_cliente_id(cliente_id, tipo_guia, competencia_alt)
            if not obrigacao and tipo_guia and dados["cnpj"]:
                obrigacao = buscar_obrigacao_por_cnpj_direto(dados["cnpj"], tipo_guia, competencia_alt)

            # Tenta match genérico com competência alternativa
            if not obrigacao:
                obrigacao = buscar_obrigacao_generica(
                    cliente_id, dados["cnpj"], razao_social, competencia_alt, texto_pdf
                )

            if obrigacao:
                match_confianca = "baixa"  # Competência alternativa nunca é alta confiança
                dados["competencia"] = competencia_alt  # Usa a competência que deu match
                if not tipo_guia:
                    dados["tipo_guia"] = obrigacao.get("descricao", "DOCUMENTO")
                logger.info(f"   Match com competência ALT ({competencia_alt}): '{obrigacao.get('descricao')}' (id={obrigacao['id']}) [confiança: BAIXA]")

        # Se achou obrigação mas não tem razao_social, pega do evento
        if obrigacao and not razao_social:
            razao_social = obrigacao.get("cliente", "Desconhecido")

        if not obrigacao:
            msg = (
                f"Obrigação pendente não encontrada: "
                f"{razao_social or 'CNPJ não encontrado'} / {tipo_guia or 'tipo desconhecido'} / {competencia or 'sem competência'}"
            )
            logger.warning(f"[AVISO] {nome}: {msg}")
            dados["_razao_social"] = razao_social
            _copiar_para_erro(caminho_pdf, msg, dados)
            return

        logger.info(
            f"   Obrigação encontrada: #{obrigacao['id']} - "
            f"{obrigacao.get('descricao')} ({obrigacao.get('competencia')}) [confiança: {match_confianca}]"
        )

        # ===== DECISÃO: BAIXA AUTOMÁTICA vs TRIAGEM =====
        # Só faz baixa automática se confiança ALTA (REGRAS_GUIAS + cliente_id exato)
        # Confiança média/baixa → manda pra triagem com sugestão de match
        if match_confianca != "alta":
            motivo_triagem = (
                f"Match com confiança {match_confianca}: "
                f"{razao_social} / {dados['tipo_guia']} / {dados['competencia']} → "
                f"sugestão: #{obrigacao['id']} ({obrigacao.get('descricao')})"
            )
            logger.info(f"[TRIAGEM] {nome}: Confiança insuficiente para baixa automática → enviando para triagem")
            dados["_razao_social"] = razao_social
            dados["_match_sugerido"] = {
                "evento_id": obrigacao["id"],
                "descricao": obrigacao.get("descricao"),
                "competencia": obrigacao.get("competencia"),
                "confianca": match_confianca,
            }
            _copiar_para_erro(caminho_pdf, motivo_triagem, dados)
            return

        # 5. Upload do PDF (só chega aqui com confiança ALTA)
        pdf_url = upload_pdf(caminho_pdf, razao_social, dados["tipo_guia"])
        if not pdf_url:
            msg = "Falha no upload do PDF"
            logger.error(f"[ERRO] {nome}: {msg}")
            dados["_razao_social"] = razao_social
            _copiar_para_erro(caminho_pdf, msg, dados)
            return

        logger.info(f"   Upload OK: {pdf_url[:80]}...")

        # 6. Dar baixa na obrigação
        sucesso = dar_baixa_obrigacao(obrigacao, pdf_url, dados)
        if sucesso:
            sem_mov_str = " [SEM MOVIMENTO]" if dados.get("sem_movimento") else ""
            logger.info(f"[OK] {nome}: Baixa realizada!{sem_mov_str} ({razao_social} - {dados['tipo_guia']})")
            _copiar_para_processados(caminho_pdf)
            # Arquiva uma cópia na pasta do cliente no servidor (bônus, nunca quebra a baixa)
            _arquivar_guia_servidor(caminho_pdf, razao_social, dados["tipo_guia"], dados["competencia"])
            stats["processados"] += 1

            # Salva log de sucesso no banco
            _registrar_log_processamento(
                evento_id=obrigacao["id"],
                cliente=razao_social,
                tipo_guia=dados["tipo_guia"],
                competencia=dados["competencia"],
                arquivo=nome,
                status="sucesso",
            )
        else:
            msg = "Falha ao atualizar obrigação no banco"
            logger.error(f"[ERRO] {nome}: {msg}")
            dados["_razao_social"] = razao_social
            _copiar_para_erro(caminho_pdf, msg, dados)

    except Exception as e:
        logger.error(f"[ERRO] {nome}: Erro inesperado - {e}", exc_info=True)
        _copiar_para_erro(caminho_pdf, str(e), dados if isinstance(dados, dict) else {})


def _copiar_para_processados(caminho: Path) -> None:
    """Copia o PDF para _processados (original fica na pasta)."""
    nome_limpo = _nome_base(caminho.name)
    destino = PASTA_PROCESSADOS / f"{date.today().isoformat()}_{nome_limpo}"
    shutil.copy2(str(caminho), str(destino))
    # Marca TODOS os nomes variantes como processados (impede loop)
    _arquivos_processados.add(caminho.name)
    _arquivos_processados.add(destino.name)
    _arquivos_processados.add(nome_limpo)
    logger.info(f"   Copiado para: _processados/{destino.name} (original mantido)")


# ============================================================
# ARQUIVAMENTO NA PASTA DO CLIENTE NO SERVIDOR
# Árvore: \\SERVIDOR\clientes B&A\[Cliente]\Guias\[Tipo]\[Ano]\RAZAO - TIPO - MM-AAAA.pdf
# ============================================================

_ILEGAIS_WIN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

def _sanitizar_componente_win(texto: str) -> str:
    """Nome de pasta/arquivo válido no Windows: tira chars ilegais, mantém espaços/acentos."""
    s = _ILEGAIS_WIN.sub(" ", str(texto or "")).strip()
    s = re.sub(r"\s+", " ", s)
    return s.strip(" .")  # Windows não aceita terminar em ponto/espaço


def _competencia_partes(competencia) -> tuple[str | None, str | None]:
    """Converte competência -> (ano, 'MM-AAAA'). Aceita AAAA-MM, AAAA/MM, MM-AAAA, MM/AAAA."""
    if not competencia:
        return (None, None)
    s = str(competencia).strip()
    m = re.match(r"^(\d{4})[-/](\d{2})$", s)
    if m:
        return (m.group(1), f"{m.group(2)}-{m.group(1)}")
    m = re.match(r"^(\d{2})[-/](\d{4})$", s)
    if m:
        return (m.group(2), f"{m.group(1)}-{m.group(2)}")
    return (None, None)


def _achar_ou_criar_subpasta_guias(pasta_cliente: Path) -> Path:
    """Reusa subpasta existente (Guias/Impostos/Tributos) se houver; senão cria 'Guias'."""
    try:
        for sub in pasta_cliente.iterdir():
            if sub.is_dir():
                n = _normalizar(sub.name)
                if "guia" in n or "imposto" in n or "tributo" in n:
                    return sub
    except (PermissionError, OSError):
        pass
    alvo = pasta_cliente / "Guias"
    alvo.mkdir(parents=True, exist_ok=True)
    return alvo


def _arquivar_guia_servidor(arquivo_local: Path, razao_social: str,
                            tipo_guia: str, competencia) -> bool:
    """Copia a guia já baixada pra pasta do cliente no servidor, organizada por Tipo/Ano.
    Só arquiva se achar a pasta do cliente com confiança (>=90). Idempotente (sobrescreve).
    NUNCA lança exceção pra cima — falha aqui não pode quebrar a baixa."""
    try:
        if not PASTA_SERVIDOR.exists():
            logger.debug("   [ARQUIVO] Servidor inacessível — pulando arquivamento")
            return False
        if not razao_social:
            return False

        pasta_cliente = _resolver_pasta_cliente({"razaoSocial": razao_social, "nomeFantasia": ""}, None)

        if not pasta_cliente:
            # Não achou a pasta do cliente (>=90) — vai pra _revisar (não cria pasta de cliente nova)
            revisar = PASTA_SERVIDOR / "_revisar"
            revisar.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(arquivo_local), str(revisar / arquivo_local.name))
            logger.info(f"   [ARQUIVO] Pasta de '{razao_social}' não encontrada → _revisar")
            return False

        ano, mmaaaa = _competencia_partes(competencia)
        base = _achar_ou_criar_subpasta_guias(pasta_cliente)
        tipo_pasta = _sanitizar_componente_win(tipo_guia or "Outros") or "Outros"
        alvo = base / tipo_pasta / (ano or "Sem competência")
        alvo.mkdir(parents=True, exist_ok=True)

        comp_str = mmaaaa or "sem-competencia"
        nome = _sanitizar_componente_win(f"{razao_social} - {tipo_guia or 'Guia'} - {comp_str}")
        ext = arquivo_local.suffix.lower() or ".pdf"
        destino = alvo / f"{nome}{ext}"
        shutil.copy2(str(arquivo_local), str(destino))  # sobrescreve = idempotente
        logger.info(f"   [ARQUIVO] Guia arquivada no servidor: {destino}")
        return True
    except Exception as e:
        logger.warning(f"   [ARQUIVO] Falha ao arquivar no servidor (não crítico): {e}")
        return False


def _marcar_triagem_arquivada(rid) -> None:
    try:
        supabase.table("triagem_documentos").update({"arquivado_servidor": True}).eq("id", rid).execute()
    except Exception:
        pass


def _arquivar_triagem_resolvidos() -> None:
    """Arquiva no servidor as guias da triagem RESOLVIDAS (aprovadas) ainda não arquivadas.
    Baixa o PDF do storage (pdf_url), arquiva com a mesma função, marca arquivado_servidor.
    Processa em lotes pequenos pra não pesar."""
    if not PASTA_SERVIDOR.exists():
        return
    try:
        resp = (
            supabase.table("triagem_documentos")
            .select("id, razao_social, tipo_guia, competencia, pdf_url, status, arquivado_servidor")
            .ilike("status", "resolvid%")
            .or_("arquivado_servidor.is.null,arquivado_servidor.eq.false")
            .limit(15)
            .execute()
        )
        linhas = resp.data or []
    except Exception as e:
        logger.debug(f"[ARQUIVO-TRIAGEM] Falha ao buscar resolvidos: {e}")
        return

    if not linhas:
        return

    import urllib.request
    import tempfile
    logger.info(f"[ARQUIVO-TRIAGEM] {len(linhas)} resolvido(s) para arquivar no servidor")
    for row in linhas:
        rid = row.get("id")
        pdf_url = row.get("pdf_url")
        razao = row.get("razao_social")
        if not pdf_url or not razao:
            _marcar_triagem_arquivada(rid)  # sem como arquivar, não reprocessa pra sempre
            continue
        try:
            tmp = Path(tempfile.gettempdir()) / f"triagem_{rid}.pdf"
            urllib.request.urlretrieve(pdf_url, str(tmp))
            _arquivar_guia_servidor(tmp, razao, row.get("tipo_guia"), row.get("competencia"))
            try:
                tmp.unlink()
            except Exception:
                pass
        except Exception as e:
            logger.warning(f"[ARQUIVO-TRIAGEM] Falha id={rid}: {e}")
            continue
        _marcar_triagem_arquivada(rid)


def _copiar_para_erro(caminho: Path, motivo: str, dados: dict | None = None) -> None:
    """Move o PDF para _erro e envia para fila de triagem no Supabase."""
    try:
        nome_limpo = _nome_base(caminho.name)
        # Limitar tamanho do nome para evitar path > 260 chars no Windows
        nome_sem_ext = Path(nome_limpo).stem[:120]
        ext = Path(nome_limpo).suffix or '.pdf'
        destino = PASTA_ERRO / f"{date.today().isoformat()}_{nome_sem_ext}{ext}"
        
        # Se destino já existe, adiciona contador
        if destino.exists():
            for i in range(1, 100):
                destino = PASTA_ERRO / f"{date.today().isoformat()}_{nome_sem_ext}_{i}{ext}"
                if not destino.exists():
                    break
        
        shutil.copy2(str(caminho), str(destino))
        
        # REMOVE o original da pasta monitorada para evitar loop infinito
        try:
            caminho.unlink()
        except Exception:
            pass
        
        # Marca TODOS os nomes variantes como processados (impede loop)
        _arquivos_processados.add(caminho.name)
        _arquivos_processados.add(destino.name)
        _arquivos_processados.add(nome_limpo)
        stats["erros"] += 1

        # Salva motivo do erro em arquivo .txt junto
        erro_txt = destino.with_suffix(".txt")
        erro_txt.write_text(f"Motivo: {motivo}\nData: {datetime.now().isoformat()}\n", encoding="utf-8")
        logger.info(f"   Movido para: _erro/{destino.name}")

        # === FILA DE TRIAGEM: upload PDF + insert no Supabase ===
        try:
            _dados = dados or {}
            # Upload do PDF para Storage
            ts = int(time.time())
            storage_name = f"triagem_{ts}_{nome_sem_ext}"
            storage_name = unicodedata.normalize("NFKD", storage_name).encode("ascii", "ignore").decode("ascii")
            storage_name = re.sub(r"[^\w_-]", "", storage_name) + ".pdf"
            storage_path = f"{CONFIG['storage_path_prefix']}/triagem/{storage_name}"

            with open(destino, "rb") as f:
                supabase.storage.from_(CONFIG["bucket_storage"]).upload(
                    storage_path, f.read(),
                    file_options={"cache-control": "3600", "upsert": "true", "content-type": "application/pdf"},
                )
            pdf_url = supabase.storage.from_(CONFIG["bucket_storage"]).get_public_url(storage_path)

            # Converte valor para float se necessário
            valor_num = None
            if _dados.get("valor") is not None:
                try:
                    valor_num = float(str(_dados["valor"]).replace(",", ".").replace("R$", "").strip())
                except (ValueError, TypeError):
                    pass

            supabase.table("triagem_documentos").insert({
                "user_id": USER_ID,
                "nome_arquivo": nome_limpo,
                "tipo_guia": _dados.get("tipo_guia"),
                "cnpj": _dados.get("cnpj"),
                "razao_social": _dados.get("_razao_social"),
                "competencia": _dados.get("competencia"),
                "valor": valor_num,
                "sem_movimento": _dados.get("sem_movimento", False),
                "pdf_url": pdf_url,
                "texto_extraido": (_dados.get("texto_completo") or "")[:3000],
                "motivo": motivo[:500],
            }).execute()
            logger.info(f"   Enviado para fila de triagem")
        except Exception as te:
            logger.warning(f"   Falha ao enviar para triagem (non-fatal): {te}")
    except Exception as e:
        logger.error(f"   Falha ao mover para _erro: {e}")
        # Mesmo falhando, marca como processado pra não ficar em loop
        _arquivos_processados.add(caminho.name)
        _arquivos_processados.add(_nome_base(caminho.name))
        stats["erros"] += 1


def _registrar_log_processamento(**kwargs) -> None:
    """Registra log de processamento na tabela agente_varredura_logs."""
    try:
        supabase.table("agente_varredura_logs").insert(
            {
                "evento_id": kwargs.get("evento_id"),
                "cliente": kwargs.get("cliente"),
                "tipo_guia": kwargs.get("tipo_guia"),
                "competencia": kwargs.get("competencia"),
                "arquivo": kwargs.get("arquivo"),
                "status": kwargs.get("status"),
                "user_id": USER_ID,
                "processado_em": datetime.now(timezone.utc).isoformat(),
            }
        ).execute()
    except Exception:
        # Log no banco é opcional, não impede o fluxo
        pass


# ============================================================
# MONITOR DE PASTA (WATCHDOG)
# ============================================================


# Pastas internas que devem ser ignoradas no monitoramento recursivo
_PASTAS_IGNORAR = {"_processados", "_erro", "_logs", "erros"}

# Adiciona dinamicamente os nomes reais das pastas configuradas (Supabase pode usar nomes diferentes)
for _p in [PASTA_PROCESSADOS, PASTA_ERRO, PASTA_LOGS]:
    _PASTAS_IGNORAR.add(_p.name.lower())


def _esta_em_pasta_interna(caminho: Path) -> bool:
    """Verifica se o arquivo está numa pasta que NÃO deve ser processada.
    Usa 3 estratégias complementares para nunca deixar passar."""
    # Estratégia 1: checa TODAS as partes do caminho (case-insensitive)
    # Funciona mesmo se relative_to falhar (drives diferentes, etc.)
    for parte in caminho.parts:
        if parte.lower() in _PASTAS_IGNORAR:
            return True

    # Estratégia 2: compara string do path resolvido contra pastas configuradas
    try:
        caminho_str = str(caminho.resolve()).lower()
        for pasta in [PASTA_PROCESSADOS, PASTA_ERRO, PASTA_LOGS]:
            pasta_str = str(pasta.resolve()).lower()
            if caminho_str.startswith(pasta_str):
                return True
    except OSError:
        pass

    # Estratégia 3: relative_to com ambos resolvidos (pega junction points, etc.)
    try:
        rel = caminho.resolve().relative_to(PASTA_MONITORADA.resolve())
        for parte in rel.parts:
            if parte.lower() in _PASTAS_IGNORAR:
                return True
    except (ValueError, OSError):
        pass

    return False


class MonitorHandler(FileSystemEventHandler):
    """Detecta novos PDFs na pasta monitorada."""

    def __init__(self):
        self._processando = set()

    def _handle_new_pdf(self, event):
        """Handler unificado para on_created e on_modified (PollingObserver usa modified)."""
        if event.is_directory:
            return
        caminho = Path(event.src_path)
        if caminho.suffix.lower() != ".pdf":
            return
        # Ignora PDFs dentro de pastas internas (_processados, _erro, _logs)
        if _esta_em_pasta_interna(caminho):
            return
        # Ignora se já processado globalmente
        nome_base = _nome_base(caminho.name)
        if caminho.name in _arquivos_processados or nome_base in _arquivos_processados:
            return
        if caminho.name in self._processando:
            return

        logger.info(f"[WATCH] Evento detectado: {caminho.name}")
        # Espera o arquivo terminar de ser copiado
        self._processando.add(caminho.name)
        threading.Thread(
            target=self._aguardar_e_processar,
            args=(caminho,),
            daemon=True,
        ).start()

    def on_created(self, event):
        self._handle_new_pdf(event)

    def on_modified(self, event):
        self._handle_new_pdf(event)

    def _aguardar_e_processar(self, caminho: Path):
        """Aguarda o arquivo ficar estável (cópia finalizada) antes de processar."""
        try:
            tamanho_anterior = -1
            tentativas = 0
            while tentativas < 15:
                if not caminho.exists():
                    return
                tamanho_atual = caminho.stat().st_size
                if tamanho_atual == tamanho_anterior and tamanho_atual > 0:
                    break
                tamanho_anterior = tamanho_atual
                time.sleep(1)
                tentativas += 1

            if caminho.exists():
                processar_arquivo(caminho)
        finally:
            self._processando.discard(caminho.name)


# ============================================================
# TRAY ICON (ÍCONE NA BANDEJA DO SISTEMA)
# ============================================================


def criar_icone_tray(observer: Observer) -> None:
    """Cria ícone na bandeja do sistema Windows."""
    try:
        import pystray
        from PIL import Image, ImageDraw

        # Cria ícone verde simples (16x16)
        img = Image.new("RGB", (64, 64), color=(34, 139, 34))
        draw = ImageDraw.Draw(img)
        draw.rectangle([16, 16, 48, 48], fill=(255, 255, 255))
        draw.text((22, 20), "V", fill=(34, 139, 34))

        def on_abrir_pasta(icon, item):
            os.startfile(str(PASTA_MONITORADA))

        def on_abrir_processados(icon, item):
            os.startfile(str(PASTA_PROCESSADOS))

        def on_abrir_erros(icon, item):
            os.startfile(str(PASTA_ERRO))

        def get_status(icon, item):
            return f"{stats['processados']} processados | {stats['erros']} erros"

        def on_sair(icon, item):
            observer.stop()
            icon.stop()

        menu = pystray.Menu(
            pystray.MenuItem(
                lambda item: f"Processados: {stats['processados']} | Erros: {stats['erros']}",
                None,
                enabled=False,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("📂 Abrir pasta monitorada", on_abrir_pasta),
            pystray.MenuItem("Abrir processados", on_abrir_processados),
            pystray.MenuItem("Abrir erros", on_abrir_erros),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Sair", on_sair),
        )

        icon = pystray.Icon(
            "agente-varredura",
            img,
            "Agente de Varredura - Monitorando...",
            menu,
        )
        icon.run()

    except ImportError:
        logger.warning("pystray não instalado - rodando sem ícone na bandeja")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            observer.stop()


# ============================================================
# PROCESSAR ARQUIVOS JÁ EXISTENTES NA PASTA
# ============================================================


def processar_existentes() -> None:
    """Processa PDFs que já estão na pasta monitorada ao iniciar."""
    # Carrega nomes de arquivos já processados com sucesso (do banco)
    try:
        resp = supabase.table("agente_varredura_logs").select("arquivo").execute()
        for row in (resp.data or []):
            if row.get("arquivo"):
                _arquivos_processados.add(row["arquivo"])
                _arquivos_processados.add(_nome_base(row["arquivo"]))
        if _arquivos_processados:
            logger.info(f"   {len(_arquivos_processados)} arquivo(s) já processados anteriormente (ignorados)")
    except Exception:
        pass

    pdfs = [p for p in PASTA_MONITORADA.rglob("*.pdf") if not _esta_em_pasta_interna(p)]
    novos = [p for p in pdfs if p.name not in _arquivos_processados]
    if novos:
        logger.info(f"[SCAN] {len(novos)} PDF(s) novo(s) encontrado(s) na pasta. Processando...")
        for pdf in novos:
            processar_arquivo(pdf)
    else:
        logger.info("[SCAN] Pasta monitorada vazia (ou todos ja processados). Aguardando novos arquivos...")


# ============================================================
# MAIN
# ============================================================


# ============================================================
# LOCK DE INSTÂNCIA ÚNICA
# ============================================================

_LOCK_FILE = BASE_DIR / ".agente.lock"

def _adquirir_lock():
    """Garante que só uma instância do agente rode por vez.
    Cria .agente.lock com o PID. Se já existe, verifica se o processo ainda está vivo."""
    if _LOCK_FILE.exists():
        try:
            pid_antigo = int(_LOCK_FILE.read_text().strip())
            # Verifica se o processo ainda está rodando E é python/pythonw
            # (PIDs são reutilizados após reboot, então só checar se existe não basta)
            import subprocess
            try:
                result = subprocess.run(
                    ['tasklist', '/FI', f'PID eq {pid_antigo}', '/FO', 'CSV', '/NH'],
                    capture_output=True, text=True, timeout=5
                )
                output = result.stdout.strip().lower()
                if 'python' in output:
                    logger.warning(f"Outra instância já está rodando (PID {pid_antigo}). Saindo.")
                    sys.exit(0)
                else:
                    logger.info(f"PID {pid_antigo} não é python (ou não existe). Lock stale, removendo.")
            except Exception:
                logger.info(f"Erro verificando PID {pid_antigo}. Lock stale, removendo.")
        except (ValueError, OSError):
            pass  # Lock file inválido, pode prosseguir

    _LOCK_FILE.write_text(str(os.getpid()))
    atexit.register(_liberar_lock)
    logger.info(f"Lock adquirido (PID {os.getpid()})")

def _liberar_lock():
    """Remove o lock file ao sair."""
    try:
        if _LOCK_FILE.exists() and _LOCK_FILE.read_text().strip() == str(os.getpid()):
            _LOCK_FILE.unlink()
    except Exception:
        pass


def _scan_documentos_loop():
    """Loop periódico: escaneia servidor a cada 10min. Aprovados são processados no heartbeat (30s)."""
    time.sleep(60)  # Espera 1min após iniciar
    while True:
        try:
            stats = _executar_scan_documentos(supabase, CONFIG)
            if stats and stats.get("novos", 0) > 0:
                logger.info(f"[SCAN-DOCS] {stats['novos']} novos documentos catalogados")
            else:
                logger.debug("[SCAN-DOCS] Nenhum documento novo no servidor")
        except Exception as e:
            logger.error(f"[SCAN-DOCS] Erro no scan: {e}")

        time.sleep(600)  # 10 minutos entre scans


def _agenda_scraper_loop():
    """Executa o scraper de agenda tributária ao iniciar e depois 1x por dia."""
    INTERVALO_DIARIO = 86400  # 24h
    time.sleep(30)  # Espera 30s após iniciar
    while True:
        try:
            from scraper_agenda import modo_automatico
            logger.info("[AGENDA] Executando scraper de agenda tributária...")
            total = modo_automatico()
            logger.info(f"[AGENDA] ✅ {total} obrigações atualizadas no cache")
        except Exception as e:
            logger.error(f"[AGENDA] Erro no scraper: {e}")

        time.sleep(INTERVALO_DIARIO)


def rodar_diagnostico_pastas() -> None:
    """Modo --diagnostico: por cliente, diz se a pasta no servidor foi encontrada (>=90).
    Gera um CSV pra abrir no Excel. Não altera NADA (só leitura)."""
    import csv
    print("=" * 60)
    print("DIAGNÓSTICO DE PASTAS DO SERVIDOR")
    print(f"Servidor: {PASTA_SERVIDOR}")
    print("=" * 60)
    if not PASTA_SERVIDOR.exists():
        print(f"ERRO: servidor inacessível ({PASTA_SERVIDOR}). Conecte o drive e tente de novo.")
        return
    try:
        resp = supabase.table("clientes").select("id, razaoSocial, status").execute()
        clientes_lista = resp.data or []
    except Exception as e:
        print(f"ERRO ao buscar clientes: {e}")
        return

    saida = BASE_DIR / f"diagnostico_pastas_{date.today().isoformat()}.csv"
    achadas = faltando = 0
    with open(saida, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["Cliente", "Status", "Pasta encontrada", "Caminho"])
        for c in clientes_lista:
            razao = c.get("razaoSocial") or ""
            if not razao:
                continue
            pasta = _resolver_pasta_cliente({"razaoSocial": razao, "nomeFantasia": ""}, None)
            if pasta:
                achadas += 1
                w.writerow([razao, c.get("status") or "", "SIM", str(pasta)])
            else:
                faltando += 1
                w.writerow([razao, c.get("status") or "", "FALTANDO", ""])
    print(f"\nClientes: {len(clientes_lista)} | Pasta achada: {achadas} | Faltando: {faltando}")
    print(f"Relatório salvo em: {saida}")
    print("Abra no Excel, filtre por 'FALTANDO' e renomeie só essas pastas pra bater com a razão social.")
    print("=" * 60)


def main():
    # Modo diagnóstico: só lista pastas e sai (não inicia o agente nem pega o lock)
    if "--diagnostico" in sys.argv or "/diagnostico" in sys.argv:
        rodar_diagnostico_pastas()
        return

    _adquirir_lock()

    logger.info("=" * 60)
    logger.info("AGENTE DE VARREDURA - Iniciando...")
    logger.info(f"   Pasta monitorada: {PASTA_MONITORADA}")
    logger.info(f"   Pasta processados: {PASTA_PROCESSADOS}")
    logger.info(f"   Pasta erros: {PASTA_ERRO}")

    # Lista subpastas encontradas (ignora internas)
    subpastas = [p.name for p in PASTA_MONITORADA.iterdir()
                 if p.is_dir() and p.name.lower() not in _PASTAS_IGNORAR]
    if subpastas:
        logger.info(f"   Subpastas detectadas: {', '.join(subpastas)}")

    logger.info("=" * 60)

    # Processa arquivos que já estavam na pasta
    processar_existentes()

    # Inicia heartbeat em thread separada
    heartbeat_thread = threading.Thread(target=heartbeat_loop, daemon=True)
    heartbeat_thread.start()
    logger.info("Heartbeat ativo (ping a cada 30s)")

    # [DESATIVADO 2026-06-02] Loop de scan automatico do servidor removido.
    # Mecanismo legado: varria \\SERVIDOR\clientes B&A a cada 10min e inchava a tabela
    # documentos_pendentes (chegou a 1,5M linhas) + estourou o egress. Substituido pelo
    # Explorador ao vivo (servidor_api.py /api/explorar). A funcao _scan_documentos_loop
    # segue definida abaixo; NAO religar sem antes paginar a dedup em scan_documentos.py.

    # Inicia scraper de agenda tributária (popula cache no Supabase)
    agenda_thread = threading.Thread(target=_agenda_scraper_loop, daemon=True)
    agenda_thread.start()
    logger.info("[AGENDA] Scraper de agenda tributária ativo (executa ao iniciar + diário)")

    # Inicia servidor API local (explorador de arquivos do servidor)
    api_thread = _iniciar_servidor_api(supabase, CONFIG, porta=5123)
    if api_thread:
        logger.info("[API] Explorador de arquivos ativo em http://localhost:5123")

    # Inicia monitoramento
    # Usa PollingObserver para pastas de rede (UNC) pois Observer nativo nao recebe eventos
    def _criar_observer():
        """Cria e inicia um observer para a pasta monitorada atual."""
        h = MonitorHandler()
        pasta_str = str(PASTA_MONITORADA)
        if pasta_str.startswith('\\\\') or pasta_str.startswith('//'):
            logger.info(f'[MONITOR] Pasta de rede detectada - usando PollingObserver (intervalo: 3s)')
            obs = PollingObserver(timeout=3)
        else:
            obs = Observer()
        obs.schedule(h, pasta_str, recursive=True)
        obs.start()
        logger.info(f"[MONITOR] Monitoramento ativo: {pasta_str}")
        return obs

    observer = _criar_observer()

    # Thread que verifica config_changed e reinicia observer
    def _config_watcher():
        nonlocal observer
        while True:
            time.sleep(5)
            if stats.get("config_changed"):
                stats["config_changed"] = False
                logger.info(f"[MONITOR] Reiniciando observer para nova pasta: {PASTA_MONITORADA}")
                try:
                    observer.stop()
                    observer.join(timeout=10)
                except Exception:
                    pass
                # Processa PDFs já existentes na nova pasta
                processar_existentes()
                observer = _criar_observer()

    config_watcher_thread = threading.Thread(target=_config_watcher, daemon=True)
    config_watcher_thread.start()

    # Thread de rescan periódico (safety net para PollingObserver em rede)
    def _rescan_periodico():
        """A cada 30s, varre a pasta monitorada procurando PDFs que o watcher não detectou."""
        while True:
            time.sleep(30)  # 30 segundos
            try:
                pdfs = [p for p in PASTA_MONITORADA.rglob("*.pdf") if not _esta_em_pasta_interna(p)]
                novos = [p for p in pdfs if p.name not in _arquivos_processados and _nome_base(p.name) not in _arquivos_processados]
                if novos:
                    logger.info(f"[RESCAN] {len(novos)} PDF(s) não detectado(s) pelo watcher. Processando...")
                    for pdf in novos:
                        processar_arquivo(pdf)
            except Exception as e:
                logger.error(f"[RESCAN] Erro no rescan periódico: {e}")

    rescan_thread = threading.Thread(target=_rescan_periodico, daemon=True)
    rescan_thread.start()

    # Inicia tray icon (bloqueia a thread principal)
    criar_icone_tray(observer)

    # Se sair do tray, para o observer
    observer.stop()
    observer.join()
    logger.info("Agente encerrado.")


if __name__ == "__main__":
    main()
