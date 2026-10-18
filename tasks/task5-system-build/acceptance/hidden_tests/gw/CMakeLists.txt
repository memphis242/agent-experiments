# Black-box behaviour test of gw, built next to the component's own unit tests. All of the component's
# sources are compiled together (internal names are not relied on); the test drives the component only
# through the names the specification pins, against the behaviour model in t5_model.h.
file(GLOB T5_SUBJECTS "${CMAKE_CURRENT_SOURCE_DIR}/../fsw/src/*.c")
list(FILTER T5_SUBJECTS EXCLUDE REGEX ".*eds.*")
add_cfe_coverage_test(gw
    UNIT_NAME hidden
    TESTCASE_SOURCES "${CMAKE_CURRENT_SOURCE_DIR}/coveragetest_gw_hidden.c"
    SUBJECT_SOURCES ${T5_SUBJECTS})
target_include_directories(coverage-gw-hidden-testrunner PRIVATE ${CMAKE_CURRENT_SOURCE_DIR})
target_link_libraries(coverage-gw-hidden-testrunner pthread)
# The test carries its own reference implementation of the cmdval_lib API; it takes precedence over the
# component's stub library, which is linked automatically for the declared dependency.
target_link_options(coverage-gw-hidden-testrunner PRIVATE "-Wl,--allow-multiple-definition")
