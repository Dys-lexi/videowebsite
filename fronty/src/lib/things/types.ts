export type Video = {
    url: string
    name: string
    duration: string
    id: number
    timetaken: number
    size: number
};
export type Coolfunfacts = Array<Record<string, number | string | boolean>> | null