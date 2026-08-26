# crossforge build environment: el8 base so produced toolchains only require
# glibc >= 2.28 on the host, plus everything GCC's build needs. flex/bison are
# required because RH snapshot tarballs ship without pre-generated scanner
# sources (see README).
FROM almalinux:8

RUN dnf install -y \
        gcc gcc-c++ make tar xz bzip2 file diffutils patch flex bison \
    && dnf clean all
