@echo off
rem Builds avm_gui.exe on Windows with Qt 6 (MinGW build) and its MinGW
rem compiler, as installed by aqt into %USERPROFILE%\Qt:
rem   pip install aqtinstall cmake ninja
rem   aqt install-qt windows desktop 6.8.3 win64_mingw -O %USERPROFILE%\Qt
rem   aqt install-tool windows desktop tools_mingw1310 qt.tools.win64_mingw1310 -O %USERPROFILE%\Qt
rem CMake and Ninja: on the path (pip's), or set CMAKE= to a cmake.exe.
rem Then deploys the Qt DLLs beside the program (windeployqt): build\avm_gui.exe runs on its own.
setlocal
set "QT=%USERPROFILE%\Qt\6.8.3\mingw_64"
set "MINGW=%USERPROFILE%\Qt\Tools\mingw1310_64"
set "PATH=%QT%\bin;%MINGW%\bin;%PATH%"
if "%CMAKE%"=="" set "CMAKE=cmake"
cd /d "%~dp0"
rem AVM_BUILD_DIR: another build folder (default build); AVM_CMAKE_ARGS: more cmake options
if "%AVM_BUILD_DIR%"=="" set "AVM_BUILD_DIR=build"
"%CMAKE%" -S . -B "%AVM_BUILD_DIR%" -G Ninja %AVM_CMAKE_ARGS% -DCMAKE_BUILD_TYPE=Release -DCMAKE_PREFIX_PATH="%QT%" -DCMAKE_CXX_COMPILER=g++ || exit /b 1
"%CMAKE%" --build "%AVM_BUILD_DIR%" || exit /b 1
"%QT%\bin\windeployqt.exe" --release --no-translations --no-opengl-sw "%AVM_BUILD_DIR%\avm_gui.exe" >nul || exit /b 1
echo built %~dp0%AVM_BUILD_DIR%\avm_gui.exe
