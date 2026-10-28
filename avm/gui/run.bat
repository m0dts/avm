@echo off
rem Starts avm_gui.exe (its Qt DLLs are beside it, from build.bat).
rem   run.bat [--demo] [--touch]
start "AVM GUI" "%~dp0build\avm_gui.exe" %*
