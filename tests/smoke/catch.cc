#include <stdexcept>
#include <string_view>

extern "C" void crossforge_throw();

int main() {
    try {
        crossforge_throw();
    } catch (const std::runtime_error& error) {
        return std::string_view(error.what()) == "crossforge-exception" ? 0 : 2;
    }
    return 1;
}
