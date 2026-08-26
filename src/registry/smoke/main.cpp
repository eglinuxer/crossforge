// Loads the plugin with dlopen and catches its exceptions across the DSO
// boundary — including a type whose code/typeinfo comes from the nonshared
// archive on both sides (two static copies must still match).
#include <dlfcn.h>
#include <cstdio>
#include <filesystem>
#include <stdexcept>

int main(int argc, char** argv) {
    const char* path = argc > 1 ? argv[1] : "./libplugin.so";
    void* handle = dlopen(path, RTLD_NOW | RTLD_LOCAL);
    if (!handle) { std::printf("FAIL dlopen: %s\n", dlerror()); return 1; }

    auto fmt = (const char* (*)())dlsym(handle, "plugin_format");
    auto fc = (double (*)(const char*))dlsym(handle, "plugin_from_chars");
    auto throw_fs = (void (*)())dlsym(handle, "plugin_throw_fs_error");
    auto throw_rt = (void (*)())dlsym(handle, "plugin_throw_runtime");
    if (!fmt || !fc || !throw_fs || !throw_rt) { std::printf("FAIL dlsym\n"); return 1; }

    std::printf("plugin says: %s / from_chars=%.4f\n", fmt(), fc("3.1415"));

    bool fs_caught = false, rt_caught = false;
    try { throw_fs(); } catch (const std::filesystem::filesystem_error& e) {
        fs_caught = true; std::printf("caught filesystem_error: %s\n", e.what());
    } catch (...) { std::printf("FAIL: filesystem_error not matched by type\n"); return 1; }
    try { throw_rt(); } catch (const std::runtime_error& e) {
        rt_caught = true; std::printf("caught runtime_error: %s\n", e.what());
    } catch (...) { std::printf("FAIL: runtime_error not matched by type\n"); return 1; }

    if (fs_caught && rt_caught) { std::printf("SMOKE OK\n"); return 0; }
    return 1;
}
