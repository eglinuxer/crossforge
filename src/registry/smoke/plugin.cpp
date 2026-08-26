// Exercises nonshared-provided symbols inside a dlopen'd DSO: the DSO and the
// main executable each carry their own static copy of the nonshared objects.
#include <filesystem>
#include <format>
#include <charconv>
#include <stdexcept>
#include <string>

extern "C" const char* plugin_format() {
    static std::string s = std::format("plugin fmt {:>6.2f}", 2.71828);
    return s.c_str();
}

extern "C" double plugin_from_chars(const char* text) {
    double v{};
    std::string_view sv{text};
    std::from_chars(sv.data(), sv.data() + sv.size(), v);
    return v;
}

extern "C" void plugin_throw_fs_error() {
    throw std::filesystem::filesystem_error(
        "boundary crossing", std::make_error_code(std::errc::io_error));
}

extern "C" void plugin_throw_runtime() {
    throw std::runtime_error("plain runtime_error across the boundary");
}
