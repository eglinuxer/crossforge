__attribute__((noinline)) static float extend_half(_Float16 value) {
    return (float)value;
}

__attribute__((noinline)) static _Float16 truncate_float(float value) {
    return (_Float16)value;
}

int main(void) {
    volatile _Float16 half = truncate_float(1.5f);
    volatile float full = extend_half(half);
    return full == 1.5f ? 0 : 1;
}
