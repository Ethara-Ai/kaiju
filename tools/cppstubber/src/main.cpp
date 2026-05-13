#include "stubber.hpp"

#include "clang/Tooling/CommonOptionsParser.h"
#include "clang/Tooling/Tooling.h"
#include "llvm/Support/CommandLine.h"
#include "llvm/Support/FileSystem.h"
#include "llvm/Support/Path.h"
#include "llvm/Support/raw_ostream.h"

#include <string>
#include <system_error>
#include <vector>

using namespace clang::tooling;
using namespace llvm;

static cl::OptionCategory StubberCategory("cppstubber options");

static cl::opt<std::string> InputDir(
    "input-dir",
    cl::desc("Directory to recursively stub (alternative to compile_commands)"),
    cl::value_desc("path"), cl::cat(StubberCategory));

static cl::opt<bool> InPlace("in-place",
                             cl::desc("Modify files in place (default: false)"),
                             cl::cat(StubberCategory));

static cl::opt<bool> StubPrivate(
    "stub-private",
    cl::desc("Also stub private methods (default: true)"),
    cl::init(true), cl::cat(StubberCategory));

static cl::opt<bool> Quiet("quiet", cl::desc("Suppress per-file output"),
                           cl::cat(StubberCategory));

static bool isCppFile(StringRef path) {
    auto ext = sys::path::extension(path).lower();
    return ext == ".cpp" || ext == ".cc" || ext == ".cxx" || ext == ".c++" ||
           ext == ".hpp" || ext == ".hh" || ext == ".hxx" || ext == ".h++" ||
           ext == ".h";
}

static std::vector<std::string> collectCppFiles(StringRef dir) {
    std::vector<std::string> files;
    std::error_code ec;
    for (sys::fs::recursive_directory_iterator it(dir, ec), end;
         it != end && !ec; it.increment(ec)) {
        auto &entry = *it;
        StringRef path = entry.path();

        if (path.contains("/build/") || path.contains("/cmake-build-") ||
            path.contains("/builddir/") || path.contains("/.cache/") ||
            path.contains("/_deps/") || path.contains("/third_party/") ||
            path.contains("/vendor/") || path.contains("/extern/") ||
            path.contains("/.git/"))
            continue;

        if (isCppFile(path))
            files.push_back(path.str());
    }
    return files;
}

int main(int argc, const char **argv) {
    cppstubber::StubConfig config;

    if (argc > 1 && std::string(argv[1]).find("--input-dir") != std::string::npos) {
        cl::ParseCommandLineOptions(argc, argv, "C++ Function Stubber\n");

        config.in_place = InPlace;
        config.stub_private = StubPrivate;

        if (InputDir.empty()) {
            errs() << "Error: --input-dir is required in directory mode\n";
            return 1;
        }

        auto files = collectCppFiles(InputDir);
        if (files.empty()) {
            errs() << "No C++ files found in: " << InputDir << "\n";
            return 1;
        }

        unsigned total_stubbed = 0;
        unsigned total_skipped = 0;
        unsigned files_processed = 0;

        for (const auto &file : files) {
            std::vector<std::string> args = {"cppstubber", file, "--"};
            std::vector<const char *> argv_vec;
            for (auto &a : args)
                argv_vec.push_back(a.c_str());

            int fake_argc = static_cast<int>(argv_vec.size());
            auto parser = CommonOptionsParser::create(
                fake_argc, argv_vec.data(), StubberCategory);
            if (!parser) {
                if (!Quiet)
                    errs() << "  SKIP (parse failed): " << file << "\n";
                continue;
            }

            ClangTool tool(parser->getCompilations(),
                           parser->getSourcePathList());

            cppstubber::StubActionFactory factory(config);
            tool.run(&factory);

            total_stubbed += factory.getTotalStubs();
            total_skipped += factory.getTotalSkips();
            ++files_processed;

            if (!Quiet && factory.getTotalStubs() > 0) {
                outs() << "  STUBBED " << factory.getTotalStubs()
                       << " functions in: " << file << "\n";
            }
        }

        outs() << "\ncppstubber summary:\n"
               << "  Files processed: " << files_processed << "\n"
               << "  Functions stubbed: " << total_stubbed << "\n"
               << "  Functions skipped: " << total_skipped << "\n";

        return 0;
    }

    auto parser =
        CommonOptionsParser::create(argc, argv, StubberCategory);
    if (!parser) {
        errs() << "Error: " << toString(parser.takeError()) << "\n";
        return 1;
    }

    config.in_place = InPlace;
    config.stub_private = StubPrivate;

    ClangTool tool(parser->getCompilations(), parser->getSourcePathList());

    cppstubber::StubActionFactory factory(config);
    int result = tool.run(&factory);

    if (!Quiet) {
        outs() << "\ncppstubber summary:\n"
               << "  Functions stubbed: " << factory.getTotalStubs() << "\n"
               << "  Functions skipped: " << factory.getTotalSkips() << "\n";
    }

    return result;
}
