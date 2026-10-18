project(CFE_STOR C)

set(APP_SRC_FILES
  fsw/src/stor.c
  fsw/src/stor_tlm.c
  fsw/src/stor_files.c
  
)

if (CFE_EDS_ENABLED)
  list(APPEND APP_SRC_FILES
    fsw/src/stor_eds_dispatch.c
  )
else()
  list(APPEND APP_SRC_FILES
    fsw/src/stor_dispatch.c
  )
endif()

# Create the app module
add_cfe_app(stor ${APP_SRC_FILES})

target_include_directories(stor PUBLIC fsw/inc $<TARGET_PROPERTY:sample_app,INCLUDE_DIRECTORIES>)
# to call library-provided functions

# Add table
add_cfe_tables(stor fsw/tables/stor_tbl.c)

# If UT is enabled, then add the tests from the subdirectory
# Note that this is an app, and therefore does not provide
# stub functions, as other entities would not typically make
# direct function calls into this application.
