#define _GNU_SOURCE

#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <link.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

extern char **environ;

static int
search_name(const char *target, char **storage, const char **file)
{
    *storage = strdup(target);
    if (*storage == NULL) {
        return -1;
    }
    char *slash = strrchr(*storage, '/');
    if (slash == NULL) {
        if (setenv("PATH", ".", 1) != 0) {
            return -1;
        }
        *file = *storage;
        return 0;
    }
    *file = slash + 1;
    if (slash == *storage) {
        *slash = '\0';
        return setenv("PATH", "/", 1);
    }
    *slash = '\0';
    return setenv("PATH", *storage, 1);
}

static int
spawn_result(int result, pid_t child)
{
    if (result != 0) {
        return result == EACCES ? 77 : 78;
    }
    int status;
    if (waitpid(child, &status, 0) != child || !WIFEXITED(status)) {
        return 79;
    }
    return WEXITSTATUS(status);
}

int
main(int argc, char **argv)
{
    if (argc != 3) {
        return 2;
    }
    const char *operation = argv[1];
    const char *target = argv[2];
    char *const target_argv[] = {(char *)target, NULL};

    if (strcmp(operation, "execve") == 0) {
        execve(target, target_argv, environ);
    } else if (strcmp(operation, "execv") == 0) {
        execv(target, target_argv);
    } else if (strcmp(operation, "execl") == 0) {
        execl(target, target, (char *)NULL);
    } else if (strcmp(operation, "execle") == 0) {
        execle(target, target, (char *)NULL, environ);
    } else if (strcmp(operation, "fexecve") == 0) {
        int descriptor = open(target, O_RDONLY);
        if (descriptor < 0) {
            return 78;
        }
        fexecve(descriptor, target_argv, environ);
        close(descriptor);
    } else if (strcmp(operation, "execveat") == 0) {
        int (*call_execveat)(
            int,
            const char *,
            char *const[],
            char *const[],
            int
        ) = dlsym(RTLD_DEFAULT, "execveat");
        if (call_execveat == NULL) {
            return 78;
        }
        call_execveat(AT_FDCWD, target, target_argv, environ, 0);
    } else if (strcmp(operation, "posix_spawn") == 0) {
        pid_t child = -1;
        int result = posix_spawn(
            &child, target, NULL, NULL, target_argv, environ
        );
        return spawn_result(result, child);
    } else if (strcmp(operation, "dlopen") == 0) {
        void *handle = dlopen(target, RTLD_NOW | RTLD_LOCAL);
        if (handle != NULL) {
            dlclose(handle);
            return 0;
        }
    } else if (strcmp(operation, "dlmopen") == 0) {
        void *handle = dlmopen(LM_ID_BASE, target, RTLD_NOW | RTLD_LOCAL);
        if (handle != NULL) {
            dlclose(handle);
            return 0;
        }
    } else if (
        strcmp(operation, "execvp") == 0
        || strcmp(operation, "execvpe") == 0
        || strcmp(operation, "execlp") == 0
        || strcmp(operation, "posix_spawnp") == 0
    ) {
        char *storage = NULL;
        const char *file = NULL;
        if (search_name(target, &storage, &file) != 0) {
            free(storage);
            return 78;
        }
        char *const search_argv[] = {(char *)file, NULL};
        if (strcmp(operation, "execvp") == 0) {
            execvp(file, search_argv);
        } else if (strcmp(operation, "execvpe") == 0) {
            execvpe(file, search_argv, environ);
        } else if (strcmp(operation, "execlp") == 0) {
            execlp(file, file, (char *)NULL);
        } else {
            pid_t child = -1;
            int result = posix_spawnp(
                &child, file, NULL, NULL, search_argv, environ
            );
            free(storage);
            return spawn_result(result, child);
        }
        free(storage);
    } else {
        return 2;
    }

    return errno == EACCES ? 77 : 78;
}
