@echo off
setlocal enabledelayedexpansion
rem Build the AnkiGen Android app (android\) and drop AnkiGen.apk right here
rem in the project folder, ready to copy onto a phone by hand.
rem
rem Kept pure ASCII and never types the project's U+2800 path: %~dp0 is this
rem script's own folder, resolved at run time. It calls a full Gradle rather
rem than android\gradlew.bat, whose bootstrap classpath breaks under that path.
cd /d "%~dp0"

if not defined JAVA_HOME set "JAVA_HOME=C:\Program Files\Android\Android Studio\jbr"
if not exist "%JAVA_HOME%\bin\java.exe" (
    echo JAVA_HOME is not a JDK: "%JAVA_HOME%"
    echo Set JAVA_HOME to a JDK 17+ and run again.
    exit /b 1
)

rem Gradle: GRADLE_HOME, then one the wrapper already downloaded. The search
rem is a bare file name with /s doing the recursion: wildcards in the middle
rem of a path match nothing in cmd.
set "GRADLE="
if defined GRADLE_HOME if exist "%GRADLE_HOME%\bin\gradle.bat" set "GRADLE=%GRADLE_HOME%\bin\gradle.bat"
if not defined GRADLE for /f "delims=" %%G in ('dir /b /s "%USERPROFILE%\.gradle\wrapper\dists\gradle.bat" 2^>nul') do if not defined GRADLE set "GRADLE=%%G"
if not defined GRADLE (
    echo Could not find Gradle. Open android\ once in Android Studio, or set GRADLE_HOME.
    exit /b 1
)

echo Building the AnkiGen debug APK...
call "%GRADLE%" -p android assembleDebug --console=plain
if errorlevel 1 (
    echo.
    echo BUILD FAILED.
    exit /b 1
)

set "SRC=android\app\build\outputs\apk\debug\app-debug.apk"
if not exist "%SRC%" (
    echo.
    echo Build succeeded but the APK was not found at %SRC%
    exit /b 1
)

set "DEST=AnkiGen.apk"
copy /y "%SRC%" "%DEST%" >nul

for %%A in ("%DEST%") do set "SIZE=%%~zA"
set /a SIZEMB=!SIZE! / 1048576

echo.
echo BUILD OK
echo APK: %CD%\%DEST%  (!SIZEMB! MB^)
endlocal
