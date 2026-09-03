#include <fmt/format.h>
#include <zlib.h>

#include <array>
#include <cstdio>
#include <cstring>
#include <string>

int main() {
    const char input[] = "crossforge-vcpkg-tier1";
    std::array<unsigned char, 128> compressed{};
    uLongf compressed_size = compressed.size();
    if (compress2(compressed.data(), &compressed_size,
                  reinterpret_cast<const Bytef*>(input), sizeof(input),
                  Z_BEST_COMPRESSION) != Z_OK) {
        return 1;
    }

    std::array<unsigned char, 128> restored{};
    uLongf restored_size = restored.size();
    if (uncompress(restored.data(), &restored_size, compressed.data(),
                   compressed_size) != Z_OK ||
        restored_size != sizeof(input) ||
        std::memcmp(restored.data(), input, sizeof(input)) != 0) {
        return 2;
    }

    const std::string output = fmt::format("{}:{}", input, 42);
    std::puts(output.c_str());
    return output == "crossforge-vcpkg-tier1:42" ? 0 : 3;
}
