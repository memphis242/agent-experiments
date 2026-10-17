# Black-box behaviour test of apps/stor, built next to the application's own unit tests.
# All application sources are compiled together (internal function names are not relied on);
# the test drives STOR_AppMain through cFE/OSAL stub models.
file(GLOB STOR_HIDDEN_SUBJECTS "${PROJECT_SOURCE_DIR}/fsw/src/*.c")
list(FILTER STOR_HIDDEN_SUBJECTS EXCLUDE REGEX ".*eds.*")

add_cfe_coverage_test(stor
    UNIT_NAME hidden
    TESTCASE_SOURCES "${CMAKE_CURRENT_SOURCE_DIR}/coveragetest_stor_hidden.c"
    SUBJECT_SOURCES ${STOR_HIDDEN_SUBJECTS})
target_link_libraries(coverage-stor-hidden-testrunner pthread)
