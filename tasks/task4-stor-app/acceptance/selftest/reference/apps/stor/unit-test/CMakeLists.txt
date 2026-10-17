include_directories(../fsw/src)

foreach(SRCFILE ${APP_SRC_FILES})
    get_filename_component(UNIT_NAME "${SRCFILE}" NAME_WE)
    set(TESTS_SOURCE_FILE "${CMAKE_CURRENT_SOURCE_DIR}/coveragetest/coveragetest_${UNIT_NAME}.c")
    add_cfe_coverage_test(stor "${UNIT_NAME}" "${TESTS_SOURCE_FILE}" "../${APP_SRC_FILES}")
    target_link_libraries(coverage-stor-${UNIT_NAME}-testrunner pthread)
endforeach()
