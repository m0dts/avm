# avm_gui: AVM's GUI in C++ (Qt Widgets, Qt 5 or 6 -- whichever is found).
#   Windows:  build.bat (Qt 6 + MinGW from aqt; Opus and Codec2 built from third_party/)
#   Pi / Linux:  sudo apt install qt6-base-dev libopus-dev libcodec2-dev cmake g++ pkg-config \n#                    libavdevice-dev libavformat-dev libavcodec-dev libavfilter-dev libswscale-dev libavutil-dev
#                cmake -S . -B build && cmake --build build -j4
cmake_minimum_required(VERSION 3.16)
project(avm_gui LANGUAGES C CXX)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_AUTOMOC ON)
# ccache where it's installed (the installer adds it): a file whose source and
# flags haven't changed comes from the cache in a moment, not recompiled
find_program(AVM_CCACHE ccache)
if(AVM_CCACHE)
    set(CMAKE_C_COMPILER_LAUNCHER ${AVM_CCACHE})
    set(CMAKE_CXX_COMPILER_LAUNCHER ${AVM_CCACHE})
endif()

find_package(QT NAMES Qt6 Qt5 REQUIRED COMPONENTS Widgets Network)
find_package(Qt${QT_VERSION_MAJOR} REQUIRED COMPONENTS Widgets Network)

# Audio: Opus and Codec2 decoders. The system's on Linux (apt); built from
# third_party/ on Windows (Opus static, BSD; Codec2 shared, LGPL). miniaudio
# (third_party/miniaudio.h, public domain) plays it.
option(AVM_SYSTEM_AUDIO_LIBS "use the system's libopus / libcodec2" ${UNIX})
if(AVM_SYSTEM_AUDIO_LIBS)
    find_package(PkgConfig REQUIRED)
    pkg_check_modules(OPUS REQUIRED IMPORTED_TARGET opus)
    pkg_check_modules(CODEC2 REQUIRED IMPORTED_TARGET codec2)
    set(AUDIO_LIBS PkgConfig::OPUS PkgConfig::CODEC2)
else()
    set(OPUS_BUILD_SHARED_LIBRARY OFF CACHE BOOL "" FORCE)
    set(OPUS_BUILD_TESTING OFF CACHE BOOL "" FORCE)
    set(OPUS_BUILD_PROGRAMS OFF CACHE BOOL "" FORCE)
    add_subdirectory(third_party/opus EXCLUDE_FROM_ALL)
    set(UNITTEST OFF CACHE BOOL "" FORCE)
    set(BUILD_SHARED_LIBS ON CACHE BOOL "" FORCE)
    add_subdirectory(third_party/codec2 EXCLUDE_FROM_ALL)
    set(BUILD_SHARED_LIBS OFF CACHE BOOL "" FORCE)
    # (on our target, not codec2's: an installable target can't carry source-tree paths)
    set(AUDIO_INCLUDES ${CMAKE_CURRENT_SOURCE_DIR}/third_party/codec2/src ${CMAKE_CURRENT_BINARY_DIR}/third_party/codec2)
    set(AUDIO_LIBS opus codec2)
endif()

# Capture (camera, microphone, test sources): the FFmpeg libraries (LGPL
# builds). The system's on Linux; third_party/ffmpeg on Windows (BtbN's
# win64-lgpl-shared build: include/, lib/*.dll.a, bin/*.dll).
if(UNIX)
    find_package(PkgConfig REQUIRED)
    pkg_check_modules(FFMPEG REQUIRED IMPORTED_TARGET libavdevice libavformat libavcodec libavfilter libswscale libavutil)
    set(FFMPEG_LIBS PkgConfig::FFMPEG)
else()
    set(FFMPEG_DIR ${CMAKE_CURRENT_SOURCE_DIR}/third_party/ffmpeg)
    set(FFMPEG_INCLUDES ${FFMPEG_DIR}/include)
    foreach(l avdevice avformat avcodec avfilter swscale avutil)
        list(APPEND FFMPEG_LIBS ${FFMPEG_DIR}/lib/lib${l}.dll.a)
    endforeach()
endif()

# The AVM library (the modem, codec, framer and the receiver / transmitter
# engines: c++/build.py's), built into c++/ beside its sources, where the GUI
# loads it from (avmDir()/c++/avm): libavm.so / avm.dll. -ffp-contract=off
# keeps the results identical to the Python reference the tests compare with.
# AVM_NATIVE: tuned for this machine's CPU (off: portable, for a release build).
option(AVM_NATIVE "build the library for this machine's CPU" ON)
set(AVM_CPP_DIR ${CMAKE_CURRENT_SOURCE_DIR}/../c++)
find_package(Threads REQUIRED)
add_library(avm SHARED ${AVM_CPP_DIR}/wavelet_core.cpp ${AVM_CPP_DIR}/framing.cpp ${AVM_CPP_DIR}/ldpc.cpp
            ${AVM_CPP_DIR}/avm_engines.cpp)
target_compile_options(avm PRIVATE -O3 -ffp-contract=off -fno-fast-math)
if(AVM_NATIVE AND NOT MSVC)
    if(CMAKE_SYSTEM_PROCESSOR MATCHES "aarch64|arm")
        target_compile_options(avm PRIVATE -mcpu=native)
    else()
        target_compile_options(avm PRIVATE -march=native)
    endif()
endif()
set_target_properties(avm PROPERTIES LIBRARY_OUTPUT_DIRECTORY ${AVM_CPP_DIR} RUNTIME_OUTPUT_DIRECTORY ${AVM_CPP_DIR})
foreach(cfg Release Debug RelWithDebInfo MinSizeRel)
    string(TOUPPER ${cfg} CFG)
    set_target_properties(avm PROPERTIES LIBRARY_OUTPUT_DIRECTORY_${CFG} ${AVM_CPP_DIR} RUNTIME_OUTPUT_DIRECTORY_${CFG} ${AVM_CPP_DIR})
endforeach()
if(WIN32)
    set_target_properties(avm PROPERTIES PREFIX "")        # avm.dll, as build.py names it
    target_link_libraries(avm PRIVATE ws2_32)
else()
    target_link_libraries(avm PRIVATE Threads::Threads ${CMAKE_DL_LIBS})
endif()

add_executable(avm_gui WIN32
    main.cpp style.cpp widgets.cpp pages.cpp mainwindow.cpp media.cpp audio.cpp miniaudio.cpp capture.cpp media_tx.cpp link_plan.cpp recorder.cpp touch.cpp web.cpp updater.cpp
    style.h widgets.h pages.h mainwindow.h media.h audio.h capture.h media_tx.h link_plan.h recorder.h touch.h web.h updater.h)
target_include_directories(avm_gui PRIVATE third_party ${AUDIO_INCLUDES} ${FFMPEG_INCLUDES})
target_link_libraries(avm_gui PRIVATE Qt${QT_VERSION_MAJOR}::Widgets Qt${QT_VERSION_MAJOR}::Network ${AUDIO_LIBS} ${FFMPEG_LIBS})
add_dependencies(avm_gui avm)    # (loaded at run time, not linked: QLibrary)
# the GUI at -O2 (after the build type's -O3, so it wins): no difference you'd see,
# about a quarter less compile time on a Pi; the library keeps its -O3
if(NOT MSVC)
    target_compile_options(avm_gui PRIVATE -O2)
endif()
if(UNIX)
    target_link_libraries(avm_gui PRIVATE ${CMAKE_DL_LIBS} pthread m)   # miniaudio loads ALSA / PulseAudio at run time
endif()
if(MSVC)
    target_compile_options(avm_gui PRIVATE /utf-8)
endif()
if(WIN32 AND NOT AVM_SYSTEM_AUDIO_LIBS)
    # the Codec2 DLL beside the program
    add_custom_command(TARGET avm_gui POST_BUILD COMMAND ${CMAKE_COMMAND} -E copy_if_different
                       $<TARGET_FILE:codec2> $<TARGET_FILE_DIR:avm_gui>)
endif()
if(WIN32)
    # the FFmpeg DLLs beside the program
    file(GLOB FFMPEG_DLLS ${FFMPEG_DIR}/bin/*.dll)
    add_custom_command(TARGET avm_gui POST_BUILD COMMAND ${CMAKE_COMMAND} -E copy_if_different
                       ${FFMPEG_DLLS} $<TARGET_FILE_DIR:avm_gui>)
endif()
