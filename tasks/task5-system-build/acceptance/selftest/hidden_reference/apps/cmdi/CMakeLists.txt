project(CFE_CMDI C)
add_cfe_app(cmdi fsw/src/cmdi_app.c)
target_include_directories(cmdi PUBLIC fsw/inc)
add_cfe_app_dependency(cmdi cmdval_lib)
