project(CFE_SFI C)
add_cfe_app(sfi fsw/src/sfi_app.c fsw/src/sfi_cluster.c)
target_include_directories(sfi PUBLIC fsw/inc)
