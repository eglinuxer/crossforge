// A QtCore console program: enough to prove the target Qt links and runs,
// without needing a display on the other side of qemu.
#include <QCoreApplication>
#include <QDateTime>
#include <QJsonDocument>
#include <QJsonObject>
#include <QLibraryInfo>
#include <QString>
#include <QSysInfo>

#include <cstdio>

int main(int argc, char **argv) {
    QCoreApplication app(argc, argv);

    // Touch a few subsystems that pull in real Qt machinery rather than
    // header-only code: the meta-object system, JSON, and the ICU/text
    // paths behind QString.
    QJsonObject report;
    report["qt_runtime"] = QLibraryInfo::version().toString();
    report["qt_build"] = QStringLiteral(QT_VERSION_STR);
    report["arch"] = QSysInfo::buildCpuArchitecture();
    report["kernel"] = QSysInfo::kernelType();
    report["app"] = QCoreApplication::applicationName().isEmpty()
                        ? QStringLiteral("qt6-cross")
                        : QCoreApplication::applicationName();
    report["utf8"] = QString::fromUtf8("交叉编译").size();

    const QByteArray json =
        QJsonDocument(report).toJson(QJsonDocument::Compact);
    std::printf("%s\n", json.constData());

    // A non-zero exit if Qt reports a version it was not built against,
    // which would mean the target picked up some other Qt.
    return QLibraryInfo::version().toString() == QStringLiteral(QT_VERSION_STR)
               ? 0
               : 1;
}
