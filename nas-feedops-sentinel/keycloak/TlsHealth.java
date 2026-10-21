// Health check for the nas-keycloak container, run by docker compose as
//
//     java /opt/keycloak/healthcheck/TlsHealth.java URL CA_PEM
//
// Keycloak serves only HTTPS (docker-compose.yml), including its management
// interface on port 9000, and its image has no curl or openssl. This asks
// URL over TLS, trusting only the authority in CA_PEM (the offline root's
// root-ca.crt, instraclustr/certs/trust; Keycloak serves its certificate with
// the web TLS CA, src/web_tls.py), and exits 0 only when the answer is 200
// with "UP" in it.
import java.io.FileInputStream;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.security.KeyStore;
import java.security.cert.CertificateFactory;
import java.time.Duration;
import javax.net.ssl.SSLContext;
import javax.net.ssl.TrustManagerFactory;

public class TlsHealth {
    public static void main(String[] args) throws Exception {
        KeyStore trust = KeyStore.getInstance(KeyStore.getDefaultType());
        trust.load(null, null);
        try (FileInputStream in = new FileInputStream(args[1])) {
            trust.setCertificateEntry("ca", CertificateFactory.getInstance("X.509").generateCertificate(in));
        }
        TrustManagerFactory tmf = TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm());
        tmf.init(trust);
        SSLContext tls = SSLContext.getInstance("TLS");
        tls.init(null, tmf.getTrustManagers(), null);

        HttpClient client = HttpClient.newBuilder().sslContext(tls).connectTimeout(Duration.ofSeconds(3)).build();
        HttpResponse<String> response = client.send(
            HttpRequest.newBuilder(URI.create(args[0])).timeout(Duration.ofSeconds(4)).build(),
            HttpResponse.BodyHandlers.ofString());
        System.exit(response.statusCode() == 200 && response.body().contains("\"UP\"") ? 0 : 1);
    }
}
