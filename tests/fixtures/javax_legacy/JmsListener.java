package com.example.legacy;

import javax.jms.MessageListener;
import javax.jms.Message;
import javax.jms.JMSException;

public class JmsListener implements MessageListener {
    @Override
    public void onMessage(Message message) {
        try {
            System.out.println(message.getJMSMessageID());
        } catch (JMSException e) {
            throw new RuntimeException(e);
        }
    }
}
