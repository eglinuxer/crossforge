#include <nanobind/nanobind.h>
#include <nanobind/stl/string.h>

#include <charconv>
#include <filesystem>
#include <stdexcept>

namespace nb = nanobind;

NB_MODULE(demoxx, m) {
    // std::from_chars for doubles is a GLIBCXX_3.4.29 symbol: it must come
    // from the nonshared archive, never from the target libstdc++.
    m.def("parse_double", [](const std::string &text) {
        double value = 0;
        auto [ptr, ec] =
            std::from_chars(text.data(), text.data() + text.size(), value);
        if (ec != std::errc())
            throw std::invalid_argument("not a number: " + text);
        (void)ptr;
        return value;
    });
    // std::filesystem symbols moved into the main library in GCC 9 — also
    // nonshared territory on the el8 baseline.
    m.def("path_exists", [](const std::string &p) {
        return std::filesystem::exists(std::filesystem::path(p));
    });
}
