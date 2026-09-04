# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['agente.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        # modulos do proprio agente
        'scan_documentos', 'scraper_agenda', 'servidor_api',
        # bandeja e imagem
        'pystray', 'PIL', 'PIL.Image',
        # monitoramento de pasta
        'watchdog', 'watchdog.observers', 'watchdog.observers.polling',
        # supabase e a pilha dele
        'supabase', 'httpx', 'gotrue', 'postgrest', 'realtime', 'storage3', 'supafunc',
        # PDF — o agente.py faz `import pymupdf` sem try/except: sem isto o
        # .exe morre no boot. Faltava na lista.
        'pymupdf', 'pdfplumber',
        # OCR nativo do Windows (opcional no codigo, mas 60x mais rapido)
        'winocr',
        # API local na porta 5123. O uvicorn carrega estes por nome em tempo
        # de execucao, entao o PyInstaller nao os descobre sozinho.
        'fastapi', 'pydantic', 'uvicorn', 'uvicorn.logging', 'uvicorn.loops',
        'uvicorn.loops.auto', 'uvicorn.protocols', 'uvicorn.protocols.http',
        'uvicorn.protocols.http.auto', 'uvicorn.protocols.websockets',
        'uvicorn.protocols.websockets.auto', 'uvicorn.lifespan',
        'uvicorn.lifespan.on',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='AgenteVarredura',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
