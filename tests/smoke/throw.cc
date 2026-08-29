#include <stdexcept>

extern "C" void crossforge_throw() {
    throw std::runtime_error("crossforge-exception");
}
