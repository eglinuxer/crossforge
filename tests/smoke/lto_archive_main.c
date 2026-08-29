extern int crossforge_lto_answer(void);

int main(void) {
    return crossforge_lto_answer() == 42 ? 0 : 1;
}
