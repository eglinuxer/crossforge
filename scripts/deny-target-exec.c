#define _GNU_SOURCE

#include <dlfcn.h>
#include <elf.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static int
path_is_below(const char *path, const char *root)
{
    size_t length = strlen(root);
    return strncmp(path, root, length) == 0
        && (path[length] == '\0' || path[length] == '/');
}

static int
is_denied_target(const char *path)
{
    char resolved[PATH_MAX];
    const char *roots = getenv("CROSSFORGE_DENY_EXEC_ROOTS");
    const char *machine_text = getenv("CROSSFORGE_DENY_EXEC_MACHINE");
    if (path == NULL || roots == NULL || machine_text == NULL
        || realpath(path, resolved) == NULL) {
        return 0;
    }

    int below = 0;
    const char *start = roots;
    while (*start != '\0') {
        const char *end = strchr(start, ':');
        size_t length = end == NULL ? strlen(start) : (size_t)(end - start);
        char root[PATH_MAX];
        if (length > 0 && length < sizeof(root)) {
            memcpy(root, start, length);
            root[length] = '\0';
            if (path_is_below(resolved, root)) {
                below = 1;
                break;
            }
        }
        if (end == NULL) {
            break;
        }
        start = end + 1;
    }
    if (!below) {
        return 0;
    }

    Elf64_Ehdr header;
    int descriptor = open(resolved, O_RDONLY | O_CLOEXEC);
    if (descriptor < 0) {
        return 0;
    }
    ssize_t count = read(descriptor, &header, sizeof(header));
    close(descriptor);
    if (count != (ssize_t)sizeof(header)
        || memcmp(header.e_ident, ELFMAG, SELFMAG) != 0
        || header.e_ident[EI_CLASS] != ELFCLASS64
        || header.e_ident[EI_DATA] != ELFDATA2LSB
        || header.e_machine != (Elf64_Half)strtoul(machine_text, NULL, 10)) {
        return 0;
    }

    const char *log_path = getenv("CROSSFORGE_DENY_EXEC_LOG");
    if (log_path != NULL) {
        int log = open(log_path, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0600);
        if (log >= 0) {
            if (write(log, resolved, strlen(resolved)) >= 0) {
                (void)(write(log, "\n", 1) < 0);
            }
            close(log);
        }
    }
    return 1;
}

int
execve(const char *path, char *const argv[], char *const envp[])
{
    static int (*next_execve)(const char *, char *const[], char *const[]);
    if (is_denied_target(path)) {
        errno = EACCES;
        return -1;
    }
    if (next_execve == NULL) {
        next_execve = dlsym(RTLD_NEXT, "execve");
    }
    if (next_execve == NULL) {
        errno = ENOSYS;
        return -1;
    }
    return next_execve(path, argv, envp);
}
