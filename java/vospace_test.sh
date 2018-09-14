#!/bin/bash

P_MYSQL='/usr/bin/mysql -u dba -pdba -h localhost'

$P_MYSQL vospace_test < `dirname $0`/vospace_test.sql

prop_create='CREATE TABLE `properties` (`identifier` varchar(128) NOT NULL'
for c in $(echo "select identifier from metaproperties" | $P_MYSQL vospace -N | cut -f2 -d'#' | grep -v 'identifier'); do
    prop_create=$prop_create', `'${c}'` varchar(256) DEFAULT NULL'
done
prop_create=$prop_create', PRIMARY KEY (`identifier`) ) ENGINE=InnoDB DEFAULT CHARSET=latin1;'
echo $prop_create | $P_MYSQL vospace_test

echo "select * from properties" | $P_MYSQL vospace -N | while read -r i; do
    id=$(echo $i | cut -d' ' -f1)
    col=$(echo $i | cut -d' ' -f2 | cut -f2 -d'#')
    val=$(echo $i | cut -d' ' -f3)
        echo "INSERT INTO properties (identifier, ${col}) VALUES ('${id}', '${val}') ON DUPLICATE KEY UPDATE ${col}='${val}';" \
            | $P_MYSQL vospace_test
done
