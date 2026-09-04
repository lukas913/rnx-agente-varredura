Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "C:\Users\Lucas\OneDrive\Documentos\Trabalho\dominio online\agente-varredura"
WshShell.Run """C:\Users\Lucas\OneDrive\Documentos\Trabalho\dominio online\.venv\Scripts\python.exe"" agente.py", 0, False
