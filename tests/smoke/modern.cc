#include <charconv>
#include <filesystem>
#include <iostream>
#include <string_view>

int main() {
    constexpr std::string_view input = "42";
    int value = 0;
    const auto result = std::from_chars(input.begin(), input.end(), value);
    const auto path = std::filesystem::path("one/../two").lexically_normal();
    if (result.ec != std::errc{} || value != 42 || path != "two") {
        return 1;
    }
    std::cout << "crossforge-cxx-ok\n";
    return 0;
}
