file(GLOB T5_SUBJECTS "${CMAKE_CURRENT_SOURCE_DIR}/../fsw/src/*.c")
list(FILTER T5_SUBJECTS EXCLUDE REGEX ".*eds.*")
add_cfe_coverage_test(mode
    UNIT_NAME hidden
    TESTCASE_SOURCES "${CMAKE_CURRENT_SOURCE_DIR}/coveragetest_mode_hidden.c"
    SUBJECT_SOURCES ${T5_SUBJECTS})
target_include_directories(coverage-mode-hidden-testrunner PRIVATE ${CMAKE_CURRENT_SOURCE_DIR})
target_link_libraries(coverage-mode-hidden-testrunner pthread)
