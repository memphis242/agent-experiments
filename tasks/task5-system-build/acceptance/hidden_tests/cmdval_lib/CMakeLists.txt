# Black-box behaviour test of cmdval_lib, built next to the component's own unit tests. All of the component's
# sources are compiled together (internal names are not relied on); the test drives the component only
# through the names the specification pins, against the behaviour model in t5_model.h.
file(GLOB T5_SUBJECTS "${CMAKE_CURRENT_SOURCE_DIR}/../fsw/src/*.c")
list(FILTER T5_SUBJECTS EXCLUDE REGEX ".*eds.*")
add_cfe_coverage_test(cmdval_lib
    UNIT_NAME hidden
    TESTCASE_SOURCES "${CMAKE_CURRENT_SOURCE_DIR}/coveragetest_cmdval_lib_hidden.c"
    SUBJECT_SOURCES ${T5_SUBJECTS})
target_include_directories(coverage-cmdval_lib-hidden-testrunner PRIVATE ${CMAKE_CURRENT_SOURCE_DIR})
target_link_libraries(coverage-cmdval_lib-hidden-testrunner pthread)
