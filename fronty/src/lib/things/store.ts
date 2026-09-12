export type video = {
    name: string
    watched?: number
    liked?: boolean
}


import Dexie, { type Table } from 'dexie';

 class wee extends Dexie {

    video!: Table<video>;

    constructor() {
        super('dbsie.db');
        this.version(1).stores({
            video: '&name, watched, liked',
        });
    }
}
  export const videodb = new wee();