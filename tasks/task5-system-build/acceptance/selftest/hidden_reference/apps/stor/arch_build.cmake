###########################################################
#
# STOR platform build setup
#
# This file is evaluated as part of the "prepare" stage
# and can be used to set up prerequisites for the build,
# such as generating header files
#
###########################################################

# The list of header files that control the STOR configuration
set(STOR_PLATFORM_CONFIG_FILE_LIST
  stor_internal_cfg_values.h
  stor_platform_cfg.h
  stor_perfids.h
  stor_msgids.h
  stor_msgid_values.h
)

generate_configfile_set(${STOR_PLATFORM_CONFIG_FILE_LIST})

