# crossforge build environment: el8 base so produced toolchains only require
# glibc >= 2.28 on the host, plus everything GCC's build needs.
# - flex/bison: RH snapshot tarballs ship without pre-generated scanner sources
# - glibc-gconv-extra: without the full gconv module set, GCC's working-iconv
#   configure probe fails and cc1 loses -fexec-charset support entirely
# - dejagnu (PowerTools): `crossforge check` (GCC upstream testsuite)
FROM almalinux:8

RUN dnf install -y \
        gcc gcc-c++ make tar xz bzip2 file diffutils patch flex bison \
        glibc-gconv-extra \
    && dnf install -y --enablerepo=powertools dejagnu \
    && dnf clean all

# Static user-mode qemu for running aarch64 testsuites (EPEL8 has no
# qemu-user-static; extracted from the multiarch/qemu-user-static image).
COPY --from=multiarch/qemu-user-static /usr/bin/qemu-aarch64-static /usr/bin/qemu-aarch64
