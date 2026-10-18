###########################################################
#
# STOR mission build setup
#
# This file is evaluated as part of the "prepare" stage
# and can be used to set up prerequisites for the build,
# such as generating header files
#
###########################################################

# The list of header files that control the STOR configuration
set(STOR_MISSION_CONFIG_FILE_LIST
  stor_fcncode_values.h
  stor_interface_cfg_values.h
  stor_mission_cfg.h
  stor_perfids.h
  stor_msg.h
  stor_msgdefs.h
  stor_msgstruct.h
  stor_tbl.h
  stor_tbldefs.h
  stor_tblstruct.h
  stor_topicid_values.h
)

generate_configfile_set(${STOR_MISSION_CONFIG_FILE_LIST})

