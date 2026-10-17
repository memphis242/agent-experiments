# Replaces apps/sample_app/unit-test/CMakeLists.txt in a scratch copy of the
# tree. Builds a single coverage test that links every SAMPLE_APP source file
# (APP_SRC_FILES, set by the parent directory) with the hidden test file, with
# the cFE services replaced by UT stubs.
include_directories(../fsw/src)

set(HIDDEN_SUBJECT_SOURCES)
foreach(SRCFILE ${APP_SRC_FILES})
    list(APPEND HIDDEN_SUBJECT_SOURCES "../${SRCFILE}")
endforeach()

add_cfe_coverage_stubs(sample_app stub_placeholder.c)

add_cfe_coverage_test(sample_app
    UNIT_NAME hidden
    TESTCASE_SOURCES "${CMAKE_CURRENT_SOURCE_DIR}/coveragetest/coveragetest_sample_app_hidden.c"
    SUBJECT_SOURCES ${HIDDEN_SUBJECT_SOURCES})
add_cfe_coverage_dependency(sample_app hidden sample_app)
add_cfe_coverage_dependency(sample_app hidden sample_lib)
