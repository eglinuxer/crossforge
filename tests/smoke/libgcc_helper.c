__attribute__((noinline)) static unsigned __int128 divide_wide(
    unsigned __int128 dividend,
    unsigned __int128 divisor) {
    return dividend / divisor;
}

int main(void) {
    volatile unsigned __int128 dividend = ((unsigned __int128)1 << 100) + 17;
    volatile unsigned __int128 divisor = 3;
    volatile unsigned __int128 result = divide_wide(dividend, divisor);
    const unsigned __int128 expected =
        ((unsigned __int128)0x0000000555555555ULL << 64) |
        0x555555555555555bULL;
    return result == expected ? 0 : 1;
}
