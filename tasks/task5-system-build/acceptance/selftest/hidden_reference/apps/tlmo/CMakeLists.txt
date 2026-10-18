project(CFE_TLMO C)
add_cfe_app(tlmo fsw/src/tlmo_app.c)
target_include_directories(tlmo PUBLIC fsw/inc)
