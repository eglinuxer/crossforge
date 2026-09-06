#include <array>

extern "C" int crossforge_answer(void);

int main() {
  constexpr std::array<int, 1> expected{{42}};
  return crossforge_answer() == expected[0] ? 0 : 1;
}
