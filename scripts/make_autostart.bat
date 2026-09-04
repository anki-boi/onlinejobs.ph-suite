@echo off
schtasks /create /tn "JobHunter" /sc onlogon /tr "\"C:\Users\PC\AppData\Roaming\uv\python\cpython-3.11-windows-x86_64-none\pythonw.exe\" C:\Users\PC\Desktop\onlinejobs.ph-suite\main.py" /f
