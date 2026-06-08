package com.example.legacy;

import javax.xml.bind.JAXBContext;
import javax.xml.bind.JAXBException;
import javax.xml.bind.Marshaller;
import javax.xml.bind.annotation.XmlRootElement;

public class JaxbService {

    public String marshal(Object obj) throws JAXBException {
        JAXBContext ctx = JAXBContext.newInstance(obj.getClass());
        Marshaller m = ctx.createMarshaller();
        java.io.StringWriter sw = new java.io.StringWriter();
        m.marshal(obj, sw);
        return sw.toString();
    }
}
