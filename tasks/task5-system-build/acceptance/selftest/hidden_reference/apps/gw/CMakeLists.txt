project(CFE_GW C)
add_cfe_app(gw fsw/src/gw_app.c)
target_include_directories(gw PUBLIC fsw/inc)
add_cfe_app_dependency(gw cmdval_lib)
