#include "message.pb.h"

#include <boost/json.hpp>

#include <iostream>
#include <string>

int main() {
    crossforge::tier3::Payload payload;
    payload.set_text("crossforge-vcpkg-tier3");
    payload.set_value(42);

    std::string wire;
    if (!payload.SerializeToString(&wire)) {
        return 1;
    }
    crossforge::tier3::Payload parsed;
    if (!parsed.ParseFromString(wire)) {
        return 2;
    }

    boost::json::object result;
    result["text"] = parsed.text();
    result["value"] = parsed.value();
    std::cout << boost::json::serialize(result) << '\n';
    return 0;
}
