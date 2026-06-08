package com.example.legacy;

import javax.transaction.Transactional;
import javax.transaction.UserTransaction;

public class TransactionService {

    @Transactional
    public void processOrder(Long orderId) {
        // business logic
    }
}
