package com.example.legacy;

import javax.xml.ws.Service;
import javax.xml.ws.WebServiceClient;
import javax.xml.ws.WebEndpoint;
import javax.xml.ws.WebServiceFeature;
import javax.xml.namespace.QName;
import java.net.URL;

public class JaxWsService extends Service {

    private static final QName SERVICE_NAME =
        new QName("http://example.com/", "LegacyService");

    public JaxWsService(URL wsdlLocation) {
        super(wsdlLocation, SERVICE_NAME);
    }
}
