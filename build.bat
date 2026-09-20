@echo off
REM ============================================================
REM TagWalker build script
REM ------------------------------------------------------------
REM Produces dist\TagWalker.exe - a single-file Windows executable
REM that bundles Python, PySide6, and all of TagWalker's code and
REM data.
REM
REM *** resources\icon.ico IS PART OF THIS REPOSITORY ***
REM   It exists and is committed. If it ever appears to be missing,
REM   something went wrong with a checkout or a copy - do NOT "work
REM   around" it by building without one. See resources\README.txt.
REM
REM *** WINDOWS VERSION COMPATIBILITY - READ THIS ***
REM   The .exe is only as backward-compatible as THIS machine.
REM   PyInstaller does NOT bundle the OS. To support Windows 10 AND
REM   11 from one build, BUILD ON WINDOWS 10. A build made on Win11
REM   may fail to launch on Win10. (Win10 build runs on 10 and 11.)
REM
REM *** NO CONSOLE IN THE PACKAGED BUILD ***
REM   --windowed means stderr goes nowhere: a traceback that is
REM   visible when running from source is invisible in the .exe.
REM   That is what Help - Diagnostic Log is for. When testing a
REM   build, check that window rather than assuming silence means
REM   success.
REM
REM Prerequisites:
REM   - 64-bit Python 3.10 or newer on PATH (3.14 is known good)
REM   - You ran (once):  pip install -r requirements.txt
REM
REM Usage:
REM   Double-click this file, or from a terminal:  build.bat
REM
REM Output:
REM   dist\TagWalker.exe   (~90-160 MB, single file, no installer)
REM   including ~15 MB of bundled reference data under resources\
REM ============================================================

echo.
echo === TagWalker build ===
echo.

REM ------------------------------------------------------------
REM Pre-flight: the bundled data must be present.
REM
REM This check exists because of a real defect. The script used to
REM have TWO separate PyInstaller commands - one with an icon, one
REM without - and only the first carried --add-data. With no icon
REM present the build silently took the other path and shipped an
REM .exe containing NONE of the reference data: no tag database, no
REM co-occurrence table, no CLIP vocabulary. It launched, then
REM failed at everything that mattered.
REM
REM There is now ONE build command, and the data is checked before
REM it runs. A missing file stops the build loudly instead of
REM producing a broken executable that looks fine.
REM ------------------------------------------------------------
echo Checking bundled data...
if not exist "resources\danbooru_tags.csv"            goto :nodata
if not exist "resources\danbooru_tags_2024-11.csv"    goto :nodata
if not exist "resources\danbooru_tags_2023-04.csv"    goto :nodata
if not exist "resources\danbooru_tags_2017-06.csv"    goto :nodata
if not exist "resources\cooccurrence_danbooru.json"   goto :nodata
if not exist "resources\clip_bpe_vocab_16e6.txt.gz"   goto :nodata
if not exist "resources\reformat_protected_tags.json" goto :nodata
echo   all reference data present.

REM ------------------------------------------------------------
REM Optional multi-tokenizer files (NON-FATAL). These power the
REM Flux tokenizer options in the token counter. Unlike the files
REM above, a missing one is NOT a build error: that tokenizer just
REM shows as unavailable in the app and the others still work. This
REM only reports which ones will be bundled, so a release isn't
REM missing a Flux ruler by accident. (SDXL/CLIP needs nothing here
REM - it uses clip_bpe_vocab above.)
REM ------------------------------------------------------------
echo Checking optional Flux tokenizer files...
if exist "resources\tokenizers\t5xxl\spiece.model" (
    echo   Flux.1 T5 tokenizer present.
) else (
    echo   [skip] Flux.1 T5 - resources\tokenizers\t5xxl\spiece.model absent.
)
if exist "resources\tokenizers\qwen3\tokenizer.json" (
    echo   Flux.2 Klein Qwen3 tokenizer present.
) else (
    echo   [skip] Flux.2 Klein - resources\tokenizers\qwen3\tokenizer.json absent.
)
if exist "resources\tokenizers\mistral\tekken.json" (
    echo   Flux.2 Dev Mistral tokenizer present.
) else (
    echo   [skip] Flux.2 Dev - resources\tokenizers\mistral\tekken.json absent.
)

set "ICONFLAG="
if exist "resources\icon.ico" (
    echo   icon present.
    set "ICONFLAG=--icon resources\icon.ico"
) else (
    echo.
    echo *** resources\icon.ico IS MISSING ***
    echo This file is part of the repository and should be here.
    echo Restore it before shipping a release. The build will
    echo continue, but without a custom icon.
    echo.
)

REM Wipe previous build artifacts. PyInstaller is mostly good at
REM detecting stale state, but a clean build avoids "why is this old
REM behavior still showing up" surprises.
if exist build rmdir /s /q build
if exist dist  rmdir /s /q dist
if exist TagWalker.spec del /q TagWalker.spec

REM PyInstaller flags:
REM   --onefile   : bundle into a single .exe (no folder of DLLs)
REM   --windowed  : no console window alongside the GUI
REM   --name      : output filename (TagWalker.exe)
REM   --collect-all PySide6
REM        Bundles EVERY part of PySide6, including Qt plugins (image
REM        formats, platforms) and translations. Without it some
REM        machines report "Could not load the Qt platform plugin
REM        'windows'". It also guarantees QtNetwork is present, which
REM        the Danbooru lookups need, and the GIF/WebP image plugins
REM        the example viewer uses. Adds ~30 MB; worth it.
REM   --add-data "resources;resources"
REM        Bundles the resources folder INTO the exe. NOT optional:
REM        the tag database, co-occurrence table, CLIP vocabulary and
REM        the window icon are all read from it at runtime via
REM        sys._MEIPASS. Semicolon is the Windows separator. This also
REM        carries the multi-tokenizer files under resources\tokenizers\
REM        (T5 spiece.model, Qwen3 tokenizer.json, Mistral tekken.json).
REM   --hidden-import sentencepiece / tokenizers / regex
REM        The token counter loads these LAZILY (only when a Flux
REM        tokenizer is first used), so PyInstaller's static import scan
REM        does NOT see them and would leave them out. Declaring them
REM        here forces them into the build:
REM          sentencepiece -> Flux.1  (T5 spiece.model)
REM          tokenizers    -> Flux.2 Klein (Qwen3 tokenizer.json)
REM          regex         -> Flux.2 Dev  (Mistral tekken.json; the Tekken
REM                           pattern needs \p{...} classes stdlib re lacks)
REM        Each is small and only loads on demand; a missing one just
REM        disables that one tokenizer, so include whichever targets you
REM        ship. (We deliberately do NOT bundle mistral-common: ~80 MB
REM        for a counter core; the pure-Python core\tekken_counter.py +
REM        regex replaces it.)
REM   --noconfirm : don't prompt if dist\ already exists
REM   --clean     : clear PyInstaller's import cache first
echo.
echo Building...
python -m PyInstaller ^
    --onefile ^
    --windowed ^
    --name TagWalker ^
    --collect-all PySide6 ^
    %ICONFLAG% ^
    --add-data "resources;resources" ^
    --hidden-import sentencepiece ^
    --hidden-import tokenizers ^
    --hidden-import regex ^
    --noconfirm ^
    --clean ^
    main.py

if errorlevel 1 (
    echo.
    echo *** BUILD FAILED ***
    echo Check the error messages above. Common fixes:
    echo   - Run:  pip install -r requirements.txt
    echo   - Make sure 'python' on PATH is 64-bit Python 3.10+
    echo.
    pause
    exit /b 1
)

echo.
echo === Build complete ===
echo.
echo Output:  dist\TagWalker.exe
echo Size:
dir dist\TagWalker.exe | findstr "TagWalker.exe"
echo.
echo Before shipping, open a dataset and check these four.
echo The first three each prove one bundled file arrived; the
echo fourth exercises all three at once and is the fastest check:
echo.
echo   - Tag Referencer shows era post counts      (tag database)
echo   - "commonly tagged with" lists partners     (co-occurrence)
echo   - Tools - Token Counter reports numbers     (CLIP vocabulary)
echo   - Statistics - Dataset health draws charts  (all three)
echo.
echo Any of those blank or zero means data did not get bundled.
echo An .exe that launches is NOT evidence the data came with it.
echo.
pause
exit /b 0

:nodata
echo.
echo *** BUILD ABORTED - bundled data is missing ***
echo.
echo One or more required files are absent from resources\.
echo The build was stopped rather than produce an .exe that
echo launches and then fails at tag classification, era verdicts,
echo co-occurrence and token counting.
echo.
echo Expected in resources\:
echo   danbooru_tags.csv
echo   danbooru_tags_2024-11.csv
echo   danbooru_tags_2023-04.csv
echo   danbooru_tags_2017-06.csv
echo   cooccurrence_danbooru.json
echo   clip_bpe_vocab_16e6.txt.gz
echo   reformat_protected_tags.json
echo.
echo (icon.ico is optional - the build proceeds without it, the
echo  .exe just gets the default PyInstaller icon.)
echo.
pause
exit /b 1
