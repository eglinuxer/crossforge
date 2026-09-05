#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>

int main(int argc, char **argv) {
  void *handle;
  const char *plugin = getenv("CROSSFORGE_QT_PLUGIN");

  (void)argc;
  (void)argv;
  if (plugin == NULL || plugin[0] != '/') {
    fputs("error: expected an absolute Qt plugin path\n", stderr);
    return 2;
  }
  dlerror();
  handle = dlopen(plugin, RTLD_NOW | RTLD_LOCAL);
  if (handle == NULL) {
    fprintf(stderr, "crossforge-dlopen-error:%s\n", dlerror());
    return 3;
  }
  fprintf(stderr, "crossforge-dlopen-ok:%s\n", plugin);
  return 0;
}
