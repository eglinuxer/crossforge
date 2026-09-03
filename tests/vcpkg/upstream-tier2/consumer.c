#include <curl/curl.h>
#include <openssl/evp.h>
#include <zlib.h>

#include <stdio.h>
#include <string.h>

int main(void) {
    const unsigned char input[] = "crossforge-vcpkg-tier2";
    unsigned char digest[EVP_MAX_MD_SIZE];
    unsigned int digest_size = 0;
    unsigned char compressed[128];
    unsigned long compressed_size = sizeof(compressed);

    if (curl_global_init(CURL_GLOBAL_DEFAULT) != CURLE_OK) {
        return 1;
    }
    if (!EVP_Digest(input, sizeof(input), digest, &digest_size, EVP_sha256(),
                    NULL) ||
        digest_size != 32 ||
        compress2(compressed, &compressed_size, input, sizeof(input),
                  Z_BEST_COMPRESSION) != Z_OK) {
        curl_global_cleanup();
        return 2;
    }
    curl_global_cleanup();
    puts("crossforge-vcpkg-tier2:42");
    return 0;
}
